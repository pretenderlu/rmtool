//go:build !linux

package main

import (
	"errors"
	"os"
)

func availableBytes(string) (uint64, error) {
	return 0, errors.New("device capacity probe requires Linux")
}
func ownedDirectory(os.FileInfo) error { return errors.New("device ownership checks require Linux") }
