package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	if len(os.Args) != 2 || os.Args[1] != "--desktop-native-lease" {
		log.Fatal("only --desktop-native-lease is supported")
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)
	defer cancel()
	if err := runDesktopNative(ctx); err != nil {
		log.Fatal(err)
	}
}
