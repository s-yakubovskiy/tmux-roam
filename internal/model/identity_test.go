package model

import (
	"os/exec"
	"strings"
	"testing"
)

func TestIdentityRoundTrip(t *testing.T) {
	ids := []Identity{{"local", "/tmp/socket", "12/100", "$1", 100}, {"хост 🐱", "/tmp/<&>\n", "12/100", "$2", 42}}
	ids = append(ids, Identity{"quote\"slash\\", "/tmp/tab\t", "0/0", "$3", 0})
	for _, id := range ids {
		encoded := id.Encode()
		decoded, err := Decode(encoded)
		if err != nil || decoded != id {
			t.Fatalf("%q: %+v, %v", encoded, decoded, err)
		}
	}
}

func TestIdentityMatchesPython(t *testing.T) {
	id := Identity{"хост 🐱", "/tmp/<&>\n", "12/100", "$2", 42}
	cmd := exec.Command("python3", "-c", `import base64,json,sys;print(base64.urlsafe_b64encode(json.dumps(['хост 🐱','/tmp/<&>\n','12/100','$2',42],ensure_ascii=True,separators=(',',':')).encode()).decode().rstrip('='))`)
	out, err := cmd.Output()
	if err != nil {
		t.Skipf("Python unavailable: %v", err)
	}
	if id.Encode() != strings.TrimSpace(string(out)) {
		t.Fatalf("Go %s != Python %s", id.Encode(), out)
	}
}

func TestBadIdentity(t *testing.T) {
	for _, s := range []string{"", "abc", "!!!", strings.Repeat("A", 20000)} {
		if _, err := Decode(s); err == nil {
			t.Fatalf("accepted %q", s)
		}
	}
}
