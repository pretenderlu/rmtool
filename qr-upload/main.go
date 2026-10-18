package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"sync"
	"syscall"
	"time"
)

func main() {
	listen := flag.String("listen", "", "explicit device Wi-Fi IPv4:port (required)")
	upstream := flag.String("upstream", "http://10.11.99.1:80", "already-active device-local native importer")
	spool := flag.String("spool-parent", "/home/root", "existing private home directory for temporary uploads")
	state := flag.String("state-dir", "/run/rmtool-qr-upload", "new private directory for session.json; existing directory is refused")
	parent := flag.Int("parent-pid", 0, "optional owning UI process; stop when it exits")
	activate := flag.Bool("activate-native", false, "temporarily prepare the verified device-local importer address")
	desktop := flag.Bool("desktop-native-lease", false, "hold an exclusive native import session for rmtool")
	flag.Parse()
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)
	defer cancel()
	if *desktop {
		if *listen != "" || *activate || *parent != 0 {
			log.Fatal("desktop lease cannot start a phone receiver")
		}
		if err := runDesktopNative(ctx); err != nil {
			log.Fatal(err)
		}
		return
	}
	if *parent != 0 {
		if *parent <= 1 || os.Getppid() != *parent {
			log.Fatal("owning UI process does not match")
		}
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		go watchParent(ctx, cancel, *parent, os.Getppid, ticker.C)
	}
	start := run
	if *activate {
		if *parent <= 1 {
			log.Fatal("native activation requires the owning UI process")
		}
		start = runManaged
	}
	if err := start(ctx, *listen, *upstream, *spool, *state); err != nil {
		log.Fatal(err)
	}
}

func watchParent(ctx context.Context, cancel context.CancelFunc, parent int, current func() int, tick <-chan time.Time) {
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick:
			if current() != parent {
				cancel()
				return
			}
		}
	}
}

func run(ctx context.Context, address, upstream, spoolParent, stateDir string) error {
	if runtime.GOOS != "linux" {
		return errors.New("this experimental receiver is device-side Linux software; use go test for host checks")
	}
	ip, err := listenIP(address)
	if err != nil {
		return err
	}
	if !assignedIP(ip) {
		return errors.New("listen IP is not assigned to this device")
	}
	parent, err := filepath.EvalSymlinks(spoolParent)
	if err != nil {
		return err
	}
	if !filepath.IsAbs(parent) || len(parent) < len("/home/") || parent[:len("/home/")] != "/home/" {
		return errors.New("upload staging must be under /home, not /data or the root filesystem")
	}
	if err := privateDirectory(parent); err != nil {
		return err
	}
	spool, err := os.MkdirTemp(parent, ".rmtool-qr-upload-")
	if err != nil {
		return err
	}
	defer os.Remove(spool)
	s, err := newReceiver("http://"+address, upstream, spool)
	if err != nil {
		return err
	}
	defer s.stop()
	if !filepath.IsAbs(stateDir) || filepath.Clean(stateDir) != stateDir {
		return errors.New("session state must use an absolute, clean directory path")
	}
	if err := privateDirectory(filepath.Dir(stateDir)); err != nil {
		return err
	}
	if err := os.Mkdir(stateDir, 0700); err != nil {
		return errors.New("cannot create private session directory; inspect any existing experiment before retrying")
	}
	defer os.Remove(stateDir)
	// The UI enables the stock service only after its bind address exists.
	readyFile := filepath.Join(stateDir, "native-ready")
	if err := writeSession(readyFile, []byte("ready\n")); err != nil {
		return err
	}
	defer os.Remove(readyFile)
	if err := waitNativeImporter(ctx, s.client, upstream); err != nil {
		return err
	}
	listener, err := net.Listen("tcp4", address)
	if err != nil {
		return err
	}
	defer listener.Close()
	stateFile := filepath.Join(stateDir, "session.json")
	stateData, err := s.sessionData()
	if err != nil {
		return err
	}
	if err := writeSession(stateFile, stateData); err != nil {
		return err
	}
	defer os.Remove(stateFile)
	srv := &http.Server{
		Handler: s, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 5 * time.Minute,
		WriteTimeout: 6 * time.Minute, IdleTimeout: 15 * time.Second, MaxHeaderBytes: 16 * 1024,
		BaseContext: func(net.Listener) context.Context { return s.sessionCtx },
	}
	defer func() {
		s.stop()
		_ = srv.Close()
		s.requests.Wait()
	}()
	limited := &limitedListener{Listener: listener, slots: make(chan struct{}, 12), done: make(chan struct{})}
	result := make(chan error, 1)
	go func() { result <- srv.Serve(limited) }()
	log.Print("Experimental HTTP receiver started; unencrypted, trusted Wi-Fi only; credentials are only in the private session file")
	timer := time.NewTimer(time.Until(s.expires))
	defer timer.Stop()
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	progressTick := time.NewTicker(time.Second)
	defer progressTick.Stop()
	waiting := true
	for waiting {
		select {
		case <-ctx.Done():
			waiting = false
		case <-timer.C:
			waiting = false
		case <-ticker.C:
			if !assignedIP(ip) {
				waiting = false
			}
		case <-progressTick.C:
			data, err := s.sessionData()
			if err != nil {
				return errors.New("cannot encode private transfer status")
			}
			if !bytes.Equal(data, stateData) {
				if err := writeSession(stateFile, data); err != nil {
					return errors.New("cannot update private transfer status")
				}
				stateData = data
			}
		case err := <-result:
			return err
		}
	}
	s.stop()
	shutdownCtx, stopShutdown := context.WithTimeout(context.Background(), 3*time.Second)
	defer stopShutdown()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		_ = srv.Close()
	}
	if err := <-result; err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	log.Print("Experimental receiver stopped")
	return nil
}

func (s *receiver) sessionData() ([]byte, error) {
	return json.Marshal(struct {
		URL      string           `json:"url"`
		Expires  time.Time        `json:"expires_at"`
		Transfer transferProgress `json:"transfer"`
	}{s.origin + "/#" + s.token, s.expires, s.transferSnapshot()})
}

func writeSession(path string, data []byte) error {
	f, err := os.OpenFile(path+".tmp", os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	defer os.Remove(path + ".tmp")
	_, err = f.Write(data)
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	return os.Rename(path+".tmp", path)
}

func listenIP(address string) (net.IP, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil {
		return nil, errors.New("an explicit Wi-Fi IPv4 address and port are required")
	}
	ip := net.ParseIP(host)
	if ip == nil || ip.To4() == nil || !ip.IsPrivate() || ip.IsUnspecified() {
		return nil, errors.New("listener must bind one private Wi-Fi IPv4, not a public or wildcard address")
	}
	if port != "8765" {
		return nil, errors.New("experimental receiver uses port 8765")
	}
	return ip, nil
}

func assignedIP(ip net.IP) bool {
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return false
	}
	for _, address := range addrs {
		if network, ok := address.(*net.IPNet); ok && network.IP.Equal(ip) {
			return true
		}
	}
	return false
}

type limitedListener struct {
	net.Listener
	slots chan struct{}
	done  chan struct{}
	once  sync.Once
}

func (l *limitedListener) Accept() (net.Conn, error) {
	select {
	case l.slots <- struct{}{}:
	case <-l.done:
		return nil, net.ErrClosed
	}
	c, err := l.Listener.Accept()
	if err != nil {
		<-l.slots
		return nil, err
	}
	return &limitedConn{Conn: c, release: func() { <-l.slots }}, nil
}

func (l *limitedListener) Close() error {
	l.once.Do(func() { close(l.done) })
	return l.Listener.Close()
}

type limitedConn struct {
	net.Conn
	once    sync.Once
	release func()
}

func (c *limitedConn) Close() error {
	err := c.Conn.Close()
	c.once.Do(c.release)
	return err
}
