package picker

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/native"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
	"github.com/rivo/uniseg"
)

type Metadata struct {
	Records        []model.Record    `json:"records"`
	Errors         map[string]string `json:"errors"`
	CurrentSession string            `json:"current_session"`
	CurrentRemote  string            `json:"current_remote"`
	Columns        int               `json:"columns"`
}
type Mode struct {
	Filter  string `json:"filter"`
	Hidden  bool   `json:"hidden"`
	Columns int    `json:"columns,omitempty"`
}

func readMode(root string) Mode {
	mode := Mode{Filter: "All"}
	_ = store.ReadJSON(filepath.Join(root, "mode.json"), 4096, &mode)
	return mode
}
func readMetadata(root string) (Metadata, error) {
	var data Metadata
	err := store.ReadJSON(filepath.Join(root, "metadata.json"), 32<<20, &data)
	return data, err
}
func fit(s string, width int, right bool) string {
	s = tmux.Clean(s)
	if uniseg.StringWidth(s) > width {
		g := uniseg.NewGraphemes(s)
		shortened := ""
		for g.Next() {
			if uniseg.StringWidth(shortened+g.Str()) > width-1 {
				break
			}
			shortened += g.Str()
		}
		s = shortened + "…"
	}
	padding := strings.Repeat(" ", max(0, width-uniseg.StringWidth(s)))
	if right {
		return padding + s
	}
	return s + padding
}
func columns(width int) (int, int) {
	available := width - 6
	win := 0
	if available >= 54 {
		win = 4
	}
	extra := 14
	if win > 0 {
		extra = 16
	}
	return max(12, min(40, available-win-extra)), win
}
func row(project, windows, state string, width int) string {
	nameWidth, winWidth := columns(width)
	fields := []string{fit(project, nameWidth, false)}
	if winWidth > 0 {
		fields = append(fields, fit(windows, winWidth, true))
	}
	fields = append(fields, fit(state, 12, false))
	return strings.TrimRight(strings.Join(fields, "  "), " ")
}

func paint(text, colour string) string {
	r, _ := strconv.ParseUint(colour[1:3], 16, 8)
	g, _ := strconv.ParseUint(colour[3:5], 16, 8)
	b, _ := strconv.ParseUint(colour[5:7], 16, 8)
	return fmt.Sprintf("\x1b[38;2;%d;%d;%dm%s\x1b[0m", r, g, b, text)
}
func payload(data Metadata, c config.Config, mode Mode, deleted map[string]bool) []byte {
	order := map[string]int{}
	hostColours := map[string]string{}
	for i, h := range c.Hosts {
		order[h.ID] = i + 1
		hostColours[h.ID] = []string{"#f5a97f", "#a6da95"}[i%2]
	}
	rows := append([]model.Record(nil), data.Records...)
	sort.SliceStable(rows, func(i, j int) bool {
		a, b := rows[i], rows[j]
		if order[a.HostID] != order[b.HostID] {
			return order[a.HostID] < order[b.HostID]
		}
		if strings.ToLower(a.Name) != strings.ToLower(b.Name) {
			return strings.ToLower(a.Name) < strings.ToLower(b.Name)
		}
		return a.SessionID < b.SessionID
	})
	seen, visible := map[string]bool{}, map[string]bool{}
	var lines []string
	for _, r := range rows {
		if deleted[r.ID] || seen[r.ID] || mode.Filter == "Local" && r.HostID != "local" || mode.Filter == "Remote" && r.HostID == "local" {
			continue
		}
		hidden := false
		if r.HostID == "local" && !mode.Hidden {
			for _, pattern := range c.HideLocal {
				match, _ := filepath.Match(pattern, r.Name)
				hidden = hidden || match
			}
		}
		if hidden {
			continue
		}
		seen[r.ID] = true
		visible[r.HostID] = true
		current := r.ID == data.CurrentRemote || r.HostID == "local" && r.SessionID == data.CurrentSession
		state := "Ready"
		switch {
		case data.Errors[r.HostID] != "":
			state = "Offline"
		case current:
			state = "Current"
		case r.Source == "ended":
			state = "Ended"
		case strings.HasPrefix(r.State, "open here"):
			state = "Connected"
		case r.Source == "cached":
			state = "Checking"
		}
		host := r.HostLabel
		if r.HostID == "local" {
			host = "Local"
		}
		nameWidth, _ := columns(data.Columns)
		host = strings.TrimRight(fit(host, min(12, nameWidth/2), false), " ")
		project := " " + host + "/" + r.Name
		if current {
			project = strings.TrimRight(fit(project, nameWidth-2, false), " ") + " *"
		}
		label := row(project, fmt.Sprint(r.Windows)+"w", state, data.Columns)
		last := strings.LastIndex(label, "  ")
		if last >= 0 {
			prefix := label[:last]
			hostColour := hostColours[r.HostID]
			if hostColour == "" {
				hostColour = "#939ab7"
			}
			hostText := " " + host
			if strings.HasPrefix(prefix, hostText+"/") {
				prefix = paint(hostText, hostColour) + paint("/", "#8087a2") + prefix[len(hostText)+1:]
			}
			label = prefix + "  " + paint(label[last+2:], native.StatusColour(state))
		}
		lines = append(lines, r.ID+"\t"+label)
	}
	var hosts []string
	for host := range data.Errors {
		hosts = append(hosts, host)
	}
	sort.Strings(hosts)
	for _, host := range hosts {
		if visible[host] || mode.Filter == "Local" && host != "local" || mode.Filter == "Remote" && host == "local" {
			continue
		}
		lines = append(lines, "error:"+host+"\t"+row(host+"/Host unavailable (Ctrl-r retries)", "—", "Offline", data.Columns))
	}
	if len(lines) == 0 {
		return nil
	}
	return []byte(strings.Join(lines, "\x00") + "\x00")
}
func snapshot(ctx context.Context, root string, data Metadata, c config.Config) (bool, error) {
	mode := readMode(root)
	if mode.Columns > 0 {
		data.Columns = mode.Columns
	}
	deleted := map[string]bool{}
	if b, err := os.ReadFile(filepath.Join(root, "deleted")); err == nil {
		for _, id := range strings.Fields(string(b)) {
			deleted[id] = true
		}
	}
	bytes := payload(data, c, mode, deleted)
	path := filepath.Join(root, "rows")
	old, err := os.ReadFile(path)
	changed := err != nil || string(old) != string(bytes)
	if changed {
		if err = store.WriteTransient(ctx, path, bytes, 0600); err != nil {
			return false, err
		}
	}
	return changed, store.WriteJSONTransient(ctx, filepath.Join(root, "metadata.json"), data)
}
func Header(root string) string {
	mode := readMode(root)
	hidden := "hidden off"
	if mode.Hidden {
		hidden = "hidden on"
	}
	width := 96
	if data, err := readMetadata(root); err == nil && data.Columns > 0 {
		width = data.Columns
	}
	if mode.Columns > 0 {
		width = mode.Columns
	}
	return mode.Filter + " · " + hidden + "\n " + row(" Project", "Win", "Status", width)
}
func StateAction(ctx context.Context, c config.Config, root, action string) error {
	if action == "refresh" {
		return store.WriteTransient(ctx, filepath.Join(root, "refresh.request"), []byte("1"), 0600)
	}
	mode := readMode(root)
	if action == "hidden" {
		mode.Hidden = !mode.Hidden
	} else if action == "filter" {
		mode.Filter = map[string]string{"All": "Local", "Local": "Remote", "Remote": "All"}[mode.Filter]
	} else if action == "resize" {
		if width, err := strconv.Atoi(os.Getenv("FZF_COLUMNS")); err == nil && width > 0 && width < 10000 {
			mode.Columns = width
		}
	} else {
		return fmt.Errorf("unknown picker action")
	}
	if err := store.WriteJSONTransient(ctx, filepath.Join(root, "mode.json"), mode); err != nil {
		return err
	}
	data, err := readMetadata(root)
	if err != nil {
		return err
	}
	_, err = snapshot(ctx, root, data, c)
	if err == nil && action == "resize" {
		err = store.WriteTransient(ctx, filepath.Join(root, "columns"), []byte(strconv.Itoa(mode.Columns)+"\n"), 0600)
	}
	if err == nil {
		b, _ := os.ReadFile(filepath.Join(root, "rows"))
		_, err = os.Stdout.Write(b)
	}
	return err
}
func Details(root, id string) error {
	data, err := readMetadata(root)
	if err != nil {
		return err
	}
	for _, r := range data.Records {
		if r.ID == id {
			if failure, err := os.ReadFile(filepath.Join(root, "connection-error")); err == nil {
				fmt.Println("Last connection error:", tmux.Clean(string(failure)))
			}
			b, _ := json.MarshalIndent(r, "", "  ")
			fmt.Println(string(b))
			return nil
		}
	}
	fmt.Println(data.Errors[strings.TrimPrefix(id, "error:")])
	return nil
}
func SelectAction(ctx context.Context, root, id string) string {
	r, err := selected(root, id)
	if id == "created" {
		err = store.ReadJSON(filepath.Join(root, "created.json"), 1<<20, &r)
		_ = os.Remove(filepath.Join(root, "created.json"))
	}
	if err != nil {
		return headerAction(err.Error())
	}
	_ = store.WriteJSONTransient(ctx, filepath.Join(root, "selection.json"), os.Getenv("FZF_POS"))
	if r.HostID == "local" {
		return "accept"
	}
	binary, err := os.Executable()
	if err != nil {
		return headerAction(err.Error())
	}
	command := ssh.Join([]string{binary, "picker-connect", root, r.ID})
	return "unbind(enter,ctrl-n,f2)+" + headerAction("Connecting to "+tmux.Clean(r.HostLabel+"/"+r.Name)+"…  Esc cancels\n"+Header(root)) + "+bg-transform(" + command + ")"
}
