package main

import (
	"errors"
	"os"
	"syscall"
)

func availableBytes(dir string) (uint64, error) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(dir, &stat); err != nil {
		return 0, err
	}
	if stat.Bsize <= 0 || stat.Bavail > ^uint64(0)/uint64(stat.Bsize) {
		return 0, errors.New("invalid filesystem capacity")
	}
	return stat.Bavail * uint64(stat.Bsize), nil
}

func ownedDirectory(info os.FileInfo) error {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uint32(os.Geteuid()) {
		return errors.New("directory is not owned by current user")
	}
	return nil
}
