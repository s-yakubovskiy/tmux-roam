package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDefaultsAndHosts(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.toml")
	c, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if c.Width != "85%" || c.Icons != true || c.Concurrency != 4 || len(c.Hosts) != 0 {
		t.Fatalf("bad defaults: %+v", c)
	}
	data := `version = 1
[picker]
width = "100%"
icons = false
hide_local = ["popup-*", "hidden"]
[discovery]
concurrency = 8
[[hosts]]
id = "omni"
ssh = "user@host"
socket_name = "custom"
[[hosts]]
id = "dd"
label = "DevDocker"
ssh = "dd"
enabled = false
socket_path = "/tmp/socket"
`
	if err = os.WriteFile(path, []byte(data), 0600); err != nil {
		t.Fatal(err)
	}
	c, err = Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if c.Width != "100%" || c.Icons || c.Concurrency != 8 || len(c.Hosts) != 2 || c.Hosts[1].Enabled || c.Hosts[0].SocketName != "custom" {
		t.Fatalf("bad parsed config: %+v", c)
	}
}

func TestValidation(t *testing.T) {
	cases := []struct{ data, contains string }{
		{`oops = 1`, "unknown key oops"},
		{`version = true`, "version must be 1"},
		{`[picker]
width = "0%"`, "invalid value"},
		{`[picker]
details = 1`, "expected boolean"},
		{`[discovery]
concurrency = true`, "expected integer"},
		{`[discovery]
concurrency = 33`, "expected integer"},
		{`[[hosts]]
id = "x"`, "missing ssh"},
		{`[[hosts]]
id = "x"
ssh = "x"
[[hosts]]
id = "x"
ssh = "x"`, "duplicate id"},
		{`[[hosts]]
id = "x"
ssh = "x"
socket_name = "a"
socket_path = "/tmp/a"`, "mutually exclusive"},
		{`[[hosts]]
id = "x"
ssh = "x"
tmux_path = "-evil"`, "invalid executable"},
		{`[picker]
width = "85%"
width = "90%"`, ""},
	}
	for _, tc := range cases {
		t.Run(strings.ReplaceAll(tc.data, "\n", "/"), func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "config.toml")
			if err := os.WriteFile(path, []byte(tc.data), 0600); err != nil {
				t.Fatal(err)
			}
			_, err := Load(path)
			if err == nil {
				t.Fatal("accepted invalid config")
			}
			if tc.contains != "" && !strings.Contains(err.Error(), tc.contains) {
				t.Fatalf("got %v, wanted %q", err, tc.contains)
			}
		})
	}
}
