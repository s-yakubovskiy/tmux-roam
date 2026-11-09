package picker

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/configsync"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
)

func SyncMenu(ctx context.Context, c config.Config, id string) error {
	preferred := strings.TrimPrefix(id, "error:")
	if identity, err := model.Decode(id); err == nil {
		preferred = identity.Host
	}
	items := []string{"All enabled hosts"}
	for _, host := range c.Hosts {
		if host.Enabled {
			if host.ID == preferred {
				items = append([]string{host.ID + "\t" + host.Label}, items...)
			} else {
				items = append(items, host.ID+"\t"+host.Label)
			}
		}
	}
	choice, err := menu(ctx, "Sync tmux config", "Choose a host or all enabled hosts", items)
	if err != nil {
		return nil
	}
	hostID := strings.SplitN(choice, "\t", 2)[0]
	if choice == "All enabled hosts" {
		hostID = ""
	}
	hosts, err := configsync.Hosts(c, hostID, choice == "All enabled hosts")
	if err != nil {
		return err
	}
	path, content, err := configsync.Source("")
	if err != nil {
		return err
	}
	directory, err := os.MkdirTemp(store.RuntimeDir(), "sync-preview-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(directory)
	var preview strings.Builder
	expected := map[string]*string{}
	expectedPaths := map[string]string{}
	for _, host := range hosts {
		response, diff, err := configsync.Compare(ctx, host, c, path, content)
		if err != nil {
			return err
		}
		expected[host.ID] = response.SHA256
		expectedPaths[host.ID] = response.Path
		fmt.Fprintf(&preview, "%s\n%s\n", host.Label, diff)
	}
	previewPath := filepath.Join(directory, "diff")
	if err = os.WriteFile(previewPath, []byte(preview.String()), 0600); err != nil {
		return err
	}
	args := append([]string{"--no-sort", "--header=Review changes; Cancel is selected initially", "--preview=cat " + ssh.Quote(previewPath), "--preview-window=up:70%"}, Options(ctx, "Confirm sync", true, false, "", 96)...)
	// Override the menu's half-page movement keys after its generic bindings:
	// scrolling the diff must never move focus away from the default Cancel.
	args = append(args, "--bind=ctrl-u:preview-half-page-up,ctrl-d:preview-half-page-down")
	child := exec.CommandContext(ctx, "fzf", args...)
	child.Stdin = strings.NewReader("Cancel\nSync config\n")
	child.Stderr = os.Stderr
	child.Env = environment()
	output, err := child.Output()
	if err != nil || strings.TrimSpace(string(output)) != "Sync config" {
		return nil
	}
	for _, host := range hosts {
		response, err := configsync.Send(ctx, host, c, "sync", content, expected[host.ID], true, expectedPaths[host.ID])
		if err != nil {
			return err
		}
		fmt.Printf("%s: %s\n", host.Label, configsync.Describe(response))
	}
	return nil
}
