package ssh

import "testing"

func FuzzCommandFields(f *testing.F) {
	for _, value := range []string{"", "a b", "quotes'\"$`\\", "Юникод 👩‍💻\nline", "x;y|z"} {
		f.Add(value)
	}
	f.Fuzz(func(t *testing.T, value string) {
		args, err := Fields(Join([]string{"command", value}))
		if err != nil || len(args) != 2 || args[1] != value {
			t.Fatalf("round trip: %q %v", args, err)
		}
	})
}
