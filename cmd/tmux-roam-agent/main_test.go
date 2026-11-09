package main

import (
	"strings"
	"testing"

	"code.talos.lowlife.us/baza/tmux-roam/internal/configsync"
)

func TestRequestFramingRejectsInvalidInputBeforeMutation(t *testing.T) {
	for _, input := range []string{"", "{} {}", "{} garbage", `{"unknown":1}`, "{}" + strings.Repeat(" ", 2*configsync.Limit)} {
		if _, err := decodeRequest(strings.NewReader(input)); err == nil {
			t.Fatal("accepted malformed or oversized request")
		}
	}
	if _, err := decodeRequest(strings.NewReader("{\"protocol\":1}\n")); err != nil {
		t.Fatal(err)
	}
}
