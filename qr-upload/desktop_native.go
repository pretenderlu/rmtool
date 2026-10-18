package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"strings"
	"time"
)

const desktopControlDir = "/run/rmtool-native-import-control"

func runDesktopNative(ctx context.Context) (err error) {
	if err := privateDirectory(desktopControlDir); err != nil {
		return errors.New("native import control is not loaded")
	}
	info, err := os.Lstat(desktopControlDir + "/control.sock")
	if err != nil || info.Mode()&os.ModeSocket == 0 || info.Mode().Perm()&0077 != 0 {
		return errors.New("native import control socket is not private")
	}
	connection, err := (&net.Dialer{Timeout: 5 * time.Second}).DialContext(ctx, "unix", desktopControlDir+"/control.sock")
	if err != nil {
		return err
	}
	defer connection.Close()
	// Acquire exclusively before changing any network state.
	reader := bufio.NewReaderSize(connection, 64)
	if err := desktopExchange(connection, reader, "acquire\n", "reserved\n"); err != nil {
		return err
	}
	cleanup := func() error { return nil }
	defer func() {
		restore := desktopExchange(connection, reader, "release\n", "restored\n")
		address := cleanup()
		err = errors.Join(err, restore, address)
		if restore == nil && err == nil {
			_, err = fmt.Fprintln(os.Stdout, "restored")
		}
	}()
	cleanup, err = prepareNativeAddress(ctx)
	if err != nil {
		cleanup = func() error { return nil }
		return err
	}
	if err := desktopExchange(connection, reader, "enable\n", "ready\n"); err != nil {
		return err
	}
	if _, err := fmt.Fprintln(os.Stdout, "ready"); err != nil {
		return err
	}
	return holdDesktopLease(ctx, os.Stdin, 15*time.Second)
}

func desktopExchange(connection net.Conn, reader *bufio.Reader, command, expected string) error {
	if err := connection.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
		return err
	}
	if _, err := io.WriteString(connection, command); err != nil {
		return err
	}
	response, err := reader.ReadSlice('\n')
	if err != nil {
		return err
	}
	if string(response) != expected {
		return fmt.Errorf("native control refused operation: %s", strings.TrimSpace(string(response)))
	}
	return connection.SetDeadline(time.Time{})
}

func holdDesktopLease(ctx context.Context, input io.Reader, idle time.Duration) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	commands := make(chan string)
	go func() {
		defer close(commands)
		reader := bufio.NewReaderSize(input, 32)
		for {
			line, err := reader.ReadSlice('\n')
			if err != nil {
				return
			}
			select {
			case commands <- string(line):
			case <-ctx.Done():
				return
			}
		}
	}()
	lastHeartbeat := time.Now()
	timer := time.NewTicker(min(idle, time.Second))
	defer timer.Stop()
	for {
		select {
		case command, ok := <-commands:
			if !ok || command == "release\n" {
				return nil
			}
			if command != "keep\n" {
				return errors.New("invalid desktop lease command")
			}
			now := time.Now()
			if desktopLeaseExpired(lastHeartbeat, now, idle) {
				return errors.New("desktop lease heartbeat expired")
			}
			lastHeartbeat = now
		case <-ctx.Done():
			return ctx.Err()
		case <-timer.C:
			if desktopLeaseExpired(lastHeartbeat, time.Now(), idle) {
				return errors.New("desktop lease heartbeat expired")
			}
		}
	}
}

func desktopLeaseExpired(last, now time.Time, idle time.Duration) bool {
	// Device suspend pauses Go's monotonic clock; include suspended wall time.
	elapsed := now.Round(0).Sub(last.Round(0))
	return elapsed < 0 || elapsed >= idle
}
