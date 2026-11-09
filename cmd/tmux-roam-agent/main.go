package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"

	"code.talos.lowlife.us/baza/tmux-roam/internal/configsync"
)

func decodeRequest(input io.Reader) (configsync.Request, error) {
	var request configsync.Request
	payload, err := io.ReadAll(io.LimitReader(input, 2*configsync.Limit+1))
	if err != nil {
		return request, err
	}
	if len(payload) > 2*configsync.Limit {
		return request, fmt.Errorf("config-sync request exceeds size limit")
	}
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.DisallowUnknownFields()
	err = decoder.Decode(&request)
	if err == nil {
		var trailing any
		if decoder.Decode(&trailing) != io.EOF {
			err = fmt.Errorf("config-sync request must contain exactly one JSON value")
		}
	}
	return request, err
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)
	defer stop()
	request, err := decodeRequest(os.Stdin)
	response := configsync.Response{Protocol: configsync.Protocol}
	if err == nil {
		response, err = configsync.Handle(ctx, request)
	}
	if err != nil {
		response.Error = err.Error()
	}
	if encodeErr := json.NewEncoder(os.Stdout).Encode(response); encodeErr != nil {
		fmt.Fprintln(os.Stderr, encodeErr)
		os.Exit(2)
	}
	if err != nil {
		os.Exit(2)
	}
}
