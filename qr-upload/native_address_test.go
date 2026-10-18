package main

import (
	"net"
	"testing"
)

func TestNativeStableMatrixAndExactIdentity(t *testing.T) {
	if len(nativeFirmwares) != 10 {
		t.Fatal("expected five stable and five exact beta targets")
	}
	for _, target := range nativeFirmwares {
		machine := "reMarkable " + target.platform
		if target.platform == "rm1" || target.platform == "rm2" {
			machine = "reMarkable " + target.platform[2:] + ".0"
		}
		actual, err := nativeFirmwareFor(machine, target.architecture, target.build, target.digest)
		if err != nil || actual != target {
			t.Fatalf("target %s refused: %v", target.platform, err)
		}
		for _, mismatch := range [][4]string{
			{machine, "unknown", target.build, target.digest},
			{"unknown", target.architecture, target.build, target.digest},
			{machine, target.architecture, "0", target.digest},
			{machine, target.architecture, target.build, "0"},
		} {
			if _, err := nativeFirmwareFor(mismatch[0], mismatch[1], mismatch[2], mismatch[3]); err == nil {
				t.Fatal("mismatched target admitted", mismatch)
			}
		}
		want := "10.11.99.1/27"
		if target.build == "20261009073202" {
			want = "10.11.99.1/29"
		}
		if target.address != want {
			t.Fatal("address differs from exact stock network configuration")
		}
	}
}

func TestNativeUSBInterfaceSelection(t *testing.T) {
	valid := []net.Addr{&net.IPNet{IP: net.ParseIP("10.11.99.1"), Mask: net.CIDRMask(27, 32)}}
	conflict := []net.Addr{&net.IPNet{IP: net.ParseIP("192.168.1.1"), Mask: net.CIDRMask(24, 32)}}
	for _, tc := range []struct {
		name      string
		addresses map[string][]net.Addr
		device    string
		present   bool
		refused   bool
	}{
		{"missing", nil, "", false, true},
		{"legacy USB only", map[string][]net.Addr{"usb0": nil}, "usb0", false, false},
		{"modern USB", map[string][]net.Addr{"usb1": nil, "usb0": nil}, "usb1", false, false},
		{"reuse USB0 address", map[string][]net.Addr{"usb1": nil, "usb0": valid}, "usb0", true, false},
		{"reuse USB1 address", map[string][]net.Addr{"usb1": valid, "usb0": valid}, "usb1", true, false},
		{"USB conflict", map[string][]net.Addr{"usb1": conflict, "usb0": valid}, "", false, true},
		{"never change Wi-Fi", map[string][]net.Addr{"wlan0": valid}, "", false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			device, present, err := selectNativeInterface(tc.addresses)
			if device != tc.device || present != tc.present || (err != nil) != tc.refused {
				t.Fatalf("device=%s present=%v error=%v", device, present, err)
			}
		})
	}
}
