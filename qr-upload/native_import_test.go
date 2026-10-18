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

func TestNativeFirmwareRequiresExactBuildAndBinary(t *testing.T) {
	stable := "5ba79d1b5656df1a771217d29a8d3938c40256be53361b10a0d17cd4752807f4"
	beta := "0d4bfba6daf8f61138996af95b494d70da11654d4861a98bffe894a425b8caba"
	for _, test := range []struct {
		build, digest string
		allowed       bool
	}{
		{"20260827113527", stable, true}, {"20261009073202", beta, true},
		{"20260827113527", beta, false}, {"20261009073202", stable, false},
		{"20261009073202", "09ba8581a50d96c19b360606ee0288b8863faec620036c8eda3b2a0b5cc2d2e2", false},
		{"", beta, false}, {"20261009073202", "", false},
	} {
		_, err := nativeFirmwareFor("reMarkable Chiappa", "arm64", test.build, test.digest)
		if (err == nil) != test.allowed {
			t.Errorf("incorrect firmware support: %+v", test)
		}
	}
}

func TestNativeAddressOwnership(t *testing.T) {
	for _, test := range []struct {
		address          string
		present, refused bool
	}{
		{"", false, false}, {"10.11.99.1/27", true, false},
		{"10.11.99.1/29", true, false},
		{"fe80::1/64", false, false}, {"10.11.99.1/24", false, true},
		{"10.11.99.2/29", false, true}, {"10.11.99.1/32", false, true},
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

func TestNativeAddressRejectsMixedConfiguration(t *testing.T) {
	valid := &net.IPNet{IP: net.ParseIP("10.11.99.1"), Mask: net.CIDRMask(29, 32)}
	conflict := &net.IPNet{IP: net.ParseIP("192.168.1.2"), Mask: net.CIDRMask(24, 32)}
	for _, addresses := range [][]net.Addr{{valid, conflict}, {conflict, valid}} {
		if present, err := nativeAddressPresent(addresses); present || err == nil {
			t.Fatalf("mixed addresses accepted: present=%v err=%v", present, err)
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
