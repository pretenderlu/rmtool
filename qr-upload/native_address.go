package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"time"
)

type nativeFirmware struct {
	platform, architecture, build, digest, address string
}

var nativeFirmwares = []nativeFirmware{
	{"ferrari", "arm64", "20260827113527", "b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021", "10.11.99.1/27"},
	{"chiappa", "arm64", "20260827113527", "5ba79d1b5656df1a771217d29a8d3938c40256be53361b10a0d17cd4752807f4", "10.11.99.1/27"},
	{"tatsu", "arm64", "20260827113527", "fa674d2ca3d8002602ce4b1b92280b96bcdf2cf32b6a42ec62ae178a1fad1fe3", "10.11.99.1/27"},
	{"rm1", "arm", "20260827113527", "1f4fbb6e14650704b5b036e482da9948e73553178f6116ad464ab072f7b90117", "10.11.99.1/27"},
	{"rm2", "arm", "20260827113527", "071d85beef3ef2d4cc0e11002140b27b82a2cc04a2ed740a5669f591069b77df", "10.11.99.1/27"},
	{"chiappa", "arm64", "20261009073202", "0d4bfba6daf8f61138996af95b494d70da11654d4861a98bffe894a425b8caba", "10.11.99.1/29"},
	{"ferrari", "arm64", "20261009073202", "9d6d844d793b6242345c7c25667451620c48b2cc28986a21049fa8d534042dd4", "10.11.99.1/29"},
	{"tatsu", "arm64", "20261009073202", "3735105b7e6b8a1d8ea42c9eabe6079c0bd1c99a546ab1a9fc7870263909ce39", "10.11.99.1/29"},
	{"rm1", "arm", "20261009073202", "0c68cf7b049fb87929b5978494a1205bc558f239f77758821c67524133a3f54b", "10.11.99.1/29"},
	{"rm2", "arm", "20261009073202", "53547bc30584b9057d5b05ada6c5fb2df3561c3a097c1c2b1aae527d5ddd2e26", "10.11.99.1/29"},
}

func nativeFirmwareFor(machine, architecture, build, digest string) (nativeFirmware, error) {
	machine = strings.ToLower(strings.Trim(machine, "\x00 \r\n"))
	platform := ""
	for _, name := range []string{"ferrari", "chiappa", "tatsu"} {
		if strings.Contains(machine, name) {
			platform = name
		}
	}
	if strings.Contains(machine, "remarkable 1") {
		platform = "rm1"
	} else if strings.Contains(machine, "remarkable 2") {
		platform = "rm2"
	}
	for _, target := range nativeFirmwares {
		if target.platform == platform && target.architecture == architecture && target.build == build && target.digest == digest {
			return target, nil
		}
	}
	return nativeFirmware{}, errors.New("native activation requires an exact verified device and firmware binary")
}

func prepareNativeAddress(ctx context.Context) (func() error, error) {
	if runtime.GOOS != "linux" {
		return nil, errors.New("native activation requires Linux")
	}
	machine, err := os.ReadFile("/sys/devices/soc0/machine")
	if err != nil {
		machine, err = os.ReadFile("/proc/device-tree/model")
	}
	if err != nil {
		return nil, errors.New("could not identify the native importer platform")
	}
	firmware, err := os.ReadFile("/etc/version")
	if err != nil {
		return nil, errors.New("could not identify the native importer firmware")
	}
	xochitl, err := os.Open("/usr/bin/xochitl")
	if err != nil {
		return nil, errors.New("could not verify the native importer binary")
	}
	digest := sha256.New()
	_, err = io.Copy(digest, xochitl)
	xochitl.Close()
	if err != nil {
		return nil, err
	}
	target, err := nativeFirmwareFor(string(machine), runtime.GOARCH, strings.TrimSpace(string(firmware)), hex.EncodeToString(digest.Sum(nil)))
	if err != nil {
		return nil, err
	}
	interfaces, err := net.Interfaces()
	if err != nil {
		return nil, err
	}
	addresses := make(map[string][]net.Addr)
	for _, device := range interfaces {
		if device.Name == "usb0" || device.Name == "usb1" {
			addresses[device.Name], err = device.Addrs()
			if err != nil {
				return nil, err
			}
		}
	}
	device, exists, err := selectNativeInterface(addresses)
	if err != nil {
		return nil, err
	}
	if !exists {
		if err := changeNativeAddress(ctx, "add", target.address, device); err != nil {
			return nil, errors.New("could not prepare the device-local importer address")
		}
	}
	return func() error {
		if !exists {
			carrier, err := os.ReadFile("/sys/class/net/" + device + "/carrier")
			if err != nil {
				return errors.New("could not confirm USB cable state for address restoration")
			}
			// A cable connected during the session now owns the normal USB address.
			if strings.TrimSpace(string(carrier)) == "0" {
				return changeNativeAddress(context.Background(), "del", target.address, device)
			}
		}
		return nil
	}, nil
}

func selectNativeInterface(addresses map[string][]net.Addr) (string, bool, error) {
	candidate := ""
	for _, name := range []string{"usb1", "usb0"} {
		values, found := addresses[name]
		if !found {
			continue
		}
		present, err := nativeAddressPresent(values)
		if err != nil {
			return "", false, err
		}
		if present {
			return name, true, nil
		}
		if candidate == "" {
			candidate = name
		}
	}
	if candidate == "" {
		return "", false, errors.New("native USB interface is missing")
	}
	return candidate, false, nil
}

func nativeAddressPresent(addresses []net.Addr) (bool, error) {
	present := false
	for _, address := range addresses {
		ip, network, err := net.ParseCIDR(address.String())
		if err != nil {
			return false, errors.New("unrecognized native interface address")
		}
		if ip.To4() == nil {
			continue
		}
		ones, _ := network.Mask.Size()
		if ip.String() != "10.11.99.1" || (ones != 29 && ones != 27) {
			return false, errors.New("native interface has a conflicting IPv4 configuration")
		}
		present = true
	}
	return present, nil
}

func changeNativeAddress(ctx context.Context, action, address, device string) error {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	return exec.CommandContext(ctx, "/sbin/ip", "addr", action, address, "dev", device).Run()
}
