package main

import (
	"fmt"
	"os"
)

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
