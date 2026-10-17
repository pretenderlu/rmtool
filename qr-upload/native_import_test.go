package main

import (
	"context"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestNativeAddressOwnership(t *testing.T) {
	for _, test := range []struct {
		address          string
		present, refused bool
	}{
		{"", false, false}, {"10.11.99.1/27", true, false},
		{"fe80::1/64", false, false}, {"10.11.99.1/24", false, true},
		{"192.168.1.2/24", false, true},
	} {
		var addresses []net.Addr
		if test.address != "" {
			ip, network, _ := net.ParseCIDR(test.address)
			network.IP = ip
			addresses = append(addresses, network)
		}
		present, err := nativeAddressPresent(addresses)
		if present != test.present || (err != nil) != test.refused {
			t.Errorf("address %s: present=%v err=%v", test.address, present, err)
		}
	}
}

func TestImporterWaitsForReadinessAndCancels(t *testing.T) {
	calls := 0
	client := &http.Client{Transport: transportFunc(func(r *http.Request) (*http.Response, error) {
		calls++
		status := 503
		if calls == 2 {
			status = 200
		}
		return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader("ready"))}, nil
	})}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := waitNativeImporter(ctx, client, "http://10.11.99.1:80"); err != nil || calls != 2 {
		t.Fatalf("readiness did not wait: %v, calls=%d", err, calls)
	}
	client.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
		return nil, r.Context().Err()
	})
	cancel()
	if err := waitNativeImporter(ctx, client, "http://10.11.99.1:80"); err == nil {
		t.Fatal("canceled activation accepted")
	}
}
