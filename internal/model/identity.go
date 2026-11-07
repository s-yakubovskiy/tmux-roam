package model

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"unicode/utf16"
)

type Identity struct {
	Host, Socket, Generation, Session string
	Created                           int64
}

// Encode matches Python json.dumps(..., ensure_ascii=True, separators=(",",":"))
// followed by URL-safe base64 without padding. Go's JSON encoder differs for HTML
// characters and non-ASCII text, so the wire representation is assembled here.
func (id Identity) Encode() string {
	parts := []string{id.Host, id.Socket, id.Generation, id.Session}
	var b strings.Builder
	b.WriteByte('[')
	for _, part := range parts {
		appendASCIIString(&b, part)
		b.WriteByte(',')
	}
	b.WriteString(strconv.FormatInt(id.Created, 10))
	b.WriteByte(']')
	return base64.RawURLEncoding.EncodeToString([]byte(b.String()))
}

func appendASCIIString(b *strings.Builder, s string) {
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '\\':
			b.WriteString(`\\`)
		case '"':
			b.WriteString(`\"`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		default:
			if r >= 32 && r < 127 {
				b.WriteRune(r)
			} else if r <= 0xffff {
				fmt.Fprintf(b, `\u%04x`, r)
			} else {
				hi, lo := utf16.EncodeRune(r)
				fmt.Fprintf(b, `\u%04x\u%04x`, hi, lo)
			}
		}
	}
	b.WriteByte('"')
}

func Decode(value string) (Identity, error) {
	var id Identity
	if len(value) > 16384 {
		return id, errors.New("invalid opaque session ID")
	}
	b, err := base64.RawURLEncoding.DecodeString(value)
	if err != nil {
		return id, errors.New("invalid opaque session ID")
	}
	var parts []json.RawMessage
	if json.Unmarshal(b, &parts) != nil || len(parts) != 5 {
		return id, errors.New("invalid opaque session ID")
	}
	for i, dst := range []*string{&id.Host, &id.Socket, &id.Generation, &id.Session} {
		if len(parts[i]) == 0 || parts[i][0] != '"' || json.Unmarshal(parts[i], dst) != nil {
			return Identity{}, errors.New("invalid opaque session ID")
		}
	}
	if err = json.Unmarshal(parts[4], &id.Created); err != nil {
		return Identity{}, errors.New("invalid opaque session ID")
	}
	return id, nil
}

type Record struct {
	ID            string           `json:"id"`
	HostID        string           `json:"host_id"`
	HostLabel     string           `json:"host_label"`
	Socket        string           `json:"socket"`
	Generation    string           `json:"generation"`
	SessionID     string           `json:"session_id"`
	Created       int64            `json:"created"`
	Name          string           `json:"name"`
	Windows       int              `json:"windows"`
	Clients       int              `json:"clients"`
	Observed      float64          `json:"observed"`
	Source        string           `json:"source"`
	State         string           `json:"state"`
	WindowDetails []map[string]any `json:"window_details"`
}
