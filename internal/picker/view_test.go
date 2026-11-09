package picker

import (
	"strings"
	"testing"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"github.com/rivo/uniseg"
)

func TestGraphemeWidthsAndIdentityRows(t *testing.T) {
	for _, text := range []string{"ASCII", "e\u0301", "👩‍💻", "🇺🇸", "проект", strings.Repeat("🐱", 30)} {
		for _, width := range []int{1, 4, 12, 40} {
			result := fit(text, width, false)
			if uniseg.StringWidth(result) != width {
				t.Fatalf("%q width %d: %q", text, width, result)
			}
		}
	}
	data := Metadata{Columns: 96, CurrentSession: "$1", Records: []model.Record{
		{ID: "a", HostID: "local", Name: "e\u0301|👩‍💻", SessionID: "$1", Windows: 2},
		{ID: "b", HostID: "remote", HostLabel: "DD", Name: "project", Windows: 1},
	}}
	rows := string(payload(data, config.Config{}, Mode{Filter: "All"}, nil))
	if !strings.Contains(rows, "a\t") || !strings.Contains(rows, "b\t") || !strings.HasSuffix(rows, "\x00") || !strings.Contains(rows, " *") {
		t.Fatal(rows)
	}
	rows = string(payload(data, config.Config{}, Mode{Filter: "Local"}, map[string]bool{"a": true}))
	if rows != "" {
		t.Fatal("hidden/deleted identities leaked", rows)
	}
}
