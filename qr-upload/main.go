package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math/big"
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
	flag.Parse()
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if err := run(ctx, *listen, *upstream, *spool, *state); err != nil {
		log.Fatal(err)
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
	s, err := newReceiver("https://"+address, upstream, spool)
	if err != nil {
		return err
	}
	defer s.stop()
	// This experiment never edits networking or xochitl to make the endpoint work.
	probeCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
	req, err := http.NewRequestWithContext(probeCtx, http.MethodGet, upstream, nil)
	if err != nil {
		cancel()
		return err
	}
	response, err := s.client.Do(req)
	if err != nil {
		cancel()
		return errors.New("native importer is unavailable; no Wi-Fi receiver was opened")
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1024))
	_ = response.Body.Close()
	cancel()
	if response.StatusCode != http.StatusOK {
		return errors.New("native importer readiness check failed")
	}
	certificate, fingerprint, err := sessionCertificate(ip)
	if err != nil {
		return err
	}
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
	listener, err := net.Listen("tcp4", address)
	if err != nil {
		return err
	}
	defer listener.Close()
	stateFile := filepath.Join(stateDir, "session.json")
	stateData, err := json.MarshalIndent(struct {
		URL         string    `json:"url"`
		Expires     time.Time `json:"expires_at"`
		Fingerprint string    `json:"certificate_sha256"`
	}{s.origin + "/#" + s.token, s.expires, fingerprint}, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(stateFile+".tmp", stateData, 0600); err != nil {
		return err
	}
	defer os.Remove(stateFile + ".tmp")
	if err := os.Rename(stateFile+".tmp", stateFile); err != nil {
		return err
	}
	defer os.Remove(stateFile)
	srv := &http.Server{
		Handler: s, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 5 * time.Minute,
		WriteTimeout: 6 * time.Minute, IdleTimeout: 15 * time.Second, MaxHeaderBytes: 16 * 1024,
		TLSConfig:   &tls.Config{Certificates: []tls.Certificate{certificate}, MinVersion: tls.VersionTLS12},
		BaseContext: func(net.Listener) context.Context { return s.sessionCtx },
	}
	defer func() {
		s.stop()
		_ = srv.Close()
		s.requests.Wait()
	}()
	limited := &limitedListener{Listener: listener, slots: make(chan struct{}, 12), done: make(chan struct{})}
	result := make(chan error, 1)
	go func() { result <- srv.ServeTLS(limited, "", "") }()
	log.Print("Experimental receiver started; credentials are only in the private session file")
	timer := time.NewTimer(time.Until(s.expires))
	defer timer.Stop()
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
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

func sessionCertificate(ip net.IP) (tls.Certificate, string, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return tls.Certificate{}, "", err
	}
	template := x509.Certificate{
		SerialNumber: serial, Subject: pkix.Name{CommonName: "rmtool temporary document receiver"},
		NotBefore: time.Now().Add(-5 * time.Minute), NotAfter: time.Now().Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		IPAddresses: []net.IP{ip}, BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, &template, &template, key.Public(), key)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	digest := sha256.Sum256(der)
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}, hex.EncodeToString(digest[:]), nil
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

func privateDirectory(name string) error {
	info, err := os.Lstat(name)
	if err != nil {
		return err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0022 != 0 {
		return fmt.Errorf("unsafe writable or non-directory parent: %s", name)
	}
	return ownedDirectory(info)
}
