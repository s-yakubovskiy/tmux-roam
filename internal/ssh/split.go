package ssh

import (
	"errors"
	"strings"
)

// Fields decodes tmux's shell-quoted pane_start_command for legacy ownership
// checks. It never expands or executes shell input.
func Fields(input string) ([]string, error) {
	var fields []string
	var word strings.Builder
	var quote byte
	started := false
	for i := 0; i < len(input); i++ {
		ch := input[i]
		if quote == '\'' {
			if ch == '\'' {
				quote = 0
			} else {
				word.WriteByte(ch)
			}
			continue
		}
		if ch == '\\' {
			if i+1 >= len(input) {
				return nil, errors.New("unfinished command escape")
			}
			next := input[i+1]
			if quote == '"' && !strings.ContainsRune("$`\"\\\n", rune(next)) {
				word.WriteByte(ch)
				continue
			}
			i++
			if next != '\n' {
				word.WriteByte(next)
			}
			started = true
			continue
		}
		if quote == '"' {
			if ch == '"' {
				quote = 0
			} else {
				word.WriteByte(ch)
			}
			continue
		}
		if ch == '\'' || ch == '"' {
			quote = ch
			started = true
			continue
		}
		if strings.ContainsRune(" \t\r\n", rune(ch)) {
			if started {
				fields = append(fields, word.String())
				word.Reset()
				started = false
			}
		} else {
			word.WriteByte(ch)
			started = true
		}
	}
	if quote != 0 {
		return nil, errors.New("unfinished command quote")
	}
	if started {
		fields = append(fields, word.String())
	}
	return fields, nil
}
