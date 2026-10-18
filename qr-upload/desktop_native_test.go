package main

import (
	"bufio"
	"bytes"
	"context"
	"io"
	"net"
	"strings"
	"testing"
	"time"
)

func TestDesktopControlProtocol(t *testing.T) {
	for _, response := range []string{"reserved\n", "busy\n", strings.Repeat("x", 80) + "\n"} {
		client, server := net.Pipe()
		go func() {
			defer server.Close()
			command, _ := bufio.NewReader(server).ReadString('\n')
			if command != "acquire\n" {
				return
			}
			_, _ = io.WriteString(server, response)
			_, _ = io.Copy(io.Discard, server)
		}()
		err := desktopExchange(client, bufio.NewReaderSize(client, 64), "acquire\n", "reserved\n")
		client.Close()
		if (err == nil) != (response == "reserved\n") {
			t.Fatalf("response %q admitted incorrectly: %v", response, err)
		}
	}
}

func TestDesktopLeaseReleasesOnEOFAndExplicitStop(t *testing.T) {
	for _, input := range []string{"", "keep\nrelease\n", "release\n"} {
		if err := holdDesktopLease(context.Background(), strings.NewReader(input), time.Second); err != nil {
			t.Fatal(err)
		}
	}
	if err := holdDesktopLease(context.Background(), strings.NewReader("invalid\n"), time.Second); err == nil {
		t.Fatal("unknown control command accepted")
	}
}

func TestDesktopLeaseExpiresWhenDesktopDisappears(t *testing.T) {
	r, w := io.Pipe()
	defer r.Close()
	defer w.Close()
	start := time.Now()
	if err := holdDesktopLease(context.Background(), r, 20*time.Millisecond); err == nil {
		t.Fatal("silent desktop kept importer enabled")
	}
	if time.Since(start) > time.Second {
		t.Fatal("idle lease did not expire promptly")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := holdDesktopLease(ctx, bytes.NewReader(nil), time.Second); err != nil && err != context.Canceled {
		t.Fatal(err)
	}
}

func TestDesktopLeaseExpirationIncludesSuspendAndClockChanges(t *testing.T) {
	last := time.Now()
	idle := 15 * time.Second
	for _, tc := range []struct {
		name    string
		elapsed time.Duration
		expired bool
	}{
		{"active", 2 * time.Second, false},
		{"just before expiry", idle - time.Nanosecond, false},
		{"exact expiry", idle, true},
		{"resumed after suspend", time.Minute, true},
		{"clock moved backward", -time.Second, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if desktopLeaseExpired(last, last.Round(0).Add(tc.elapsed), idle) != tc.expired {
				t.Fatal("incorrect elapsed-wall-time admission")
			}
		})
	}
}
