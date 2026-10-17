package main

import (
	"context"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"time"
)

const nativeAddress = "10.11.99.1/27"

func runManaged(ctx context.Context, address, upstream, spool, state string) error {
	if runtime.GOOS != "linux" || upstream != "http://10.11.99.1:80" {
		return errors.New("native activation is restricted to the supported device importer")
	}
	machine, err := os.ReadFile("/sys/devices/soc0/machine")
	if err != nil || strings.TrimSpace(string(machine)) != "reMarkable Chiappa" {
		return errors.New("native activation requires the verified Move platform")
	}
	firmware, err := os.ReadFile("/etc/version")
	if err != nil || strings.TrimSpace(string(firmware)) != "20260827113527" {
		return errors.New("native activation requires the verified firmware build")
	}
	device, err := net.InterfaceByName("usb1")
	if err != nil {
		return errors.New("native USB interface is missing")
	}
	addresses, err := device.Addrs()
	if err != nil {
		return err
	}
	exists, err := nativeAddressPresent(addresses)
	if err != nil {
		return err
	}
	if !exists {
		if err := changeNativeAddress(ctx, "add"); err != nil {
			return errors.New("could not prepare the device-local importer address")
		}
		defer func() {
			// A cable connected during reception now owns the normal USB address.
			carrier, err := os.ReadFile("/sys/class/net/usb1/carrier")
			if err == nil && strings.TrimSpace(string(carrier)) == "0" {
				if err := changeNativeAddress(context.Background(), "del"); err != nil {
					log.Print("Could not remove the temporary native importer address")
				}
			}
		}()
	}
	return run(ctx, address, upstream, spool, state)
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
		if ip.String() != "10.11.99.1" || ones != 27 {
			return false, errors.New("native interface has a conflicting IPv4 configuration")
		}
		present = true
	}
	return present, nil
}

func changeNativeAddress(ctx context.Context, action string) error {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	return exec.CommandContext(ctx, "/sbin/ip", "addr", action, nativeAddress, "dev", "usb1").Run()
}

func waitNativeImporter(ctx context.Context, client *http.Client, upstream string) error {
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	for {
		probe, stop := context.WithTimeout(ctx, 2*time.Second)
		req, err := http.NewRequestWithContext(probe, http.MethodGet, upstream, nil)
		if err != nil {
			stop()
			return err
		}
		response, err := client.Do(req)
		ready := false
		if err == nil {
			_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1024))
			_ = response.Body.Close()
			ready = response.StatusCode == http.StatusOK
		}
		stop()
		if ready {
			return nil
		}
		select {
		case <-ctx.Done():
			return errors.New("native importer is unavailable; no Wi-Fi receiver was opened")
		case <-time.After(500 * time.Millisecond):
		}
	}
}
