package picker

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"

	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
)

// fzf's --track with --id-nth discards keystrokes during reload. Capture the
// opaque identity before reloading and restore it once the new matches arrive.
// Ordinary tracking still keeps the cursor stable while the query changes.
func reloadAction(root, command string) string {
	path := ssh.Quote(filepath.Join(root, "reload-selection"))
	return "rebind(focus,load)+execute-silent(test -f " + path + " || printf '%s\\0%s' {q} {1} > " + path + ")+reload-sync(" + command + ")"
}

func RestoreSelection(root, matches string) (string, error) {
	path := filepath.Join(root, "reload-selection")
	saved, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	query, id, ok := bytes.Cut(saved, []byte{0})
	if !ok || len(id) == 0 || string(query) != os.Getenv("FZF_QUERY") {
		_ = os.Remove(path)
		return "", nil // New input deliberately changes the matching selection.
	}
	rows, err := os.ReadFile(matches)
	if err != nil {
		return "", err
	}
	defer os.Remove(path)
	for i, row := range bytes.Split(rows, []byte{0}) {
		if bytes.Equal(bytes.TrimSpace(row), id) {
			return fmt.Sprintf("pos(%d)", i+1), nil
		}
	}
	return "", nil
}
