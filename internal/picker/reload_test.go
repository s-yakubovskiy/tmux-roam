package picker

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReloadRestoresIdentityWithoutOverridingNewInput(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "reload-selection")
	matches := filepath.Join(root, "matches")
	if err := os.WriteFile(matches, []byte("new\x00selected\x00other\x00"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct{ query, id, want string }{
		{"проект", "selected", "pos(2)"},
		{"проект-extra", "selected", ""},
		{"проект", "removed", ""},
	} {
		t.Setenv("FZF_QUERY", c.query)
		if err := os.WriteFile(path, []byte("проект\x00"+c.id), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := selected(root, c.id); err == nil {
			t.Fatal("selection was usable before identity restoration")
		}
		got, err := RestoreSelection(root, matches)
		if err != nil || got != c.want {
			t.Fatalf("%+v: %q, %v", c, got, err)
		}
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Fatal("reload marker remained", err)
		}
	}
}
