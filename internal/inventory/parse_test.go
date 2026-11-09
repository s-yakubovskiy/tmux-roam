package inventory

import "testing"

func FuzzSessionLine(f *testing.F) {
	for _, value := range []string{"", "$1|100|project|5|0|C-b", "$2|42|name|with|pipes|2|0", "$1|-1|bad|0|0", "$1|1|Юникод 👩‍💻|1|0|None"} {
		f.Add(value, true)
		f.Add(value, false)
	}
	f.Fuzz(func(t *testing.T, value string, remote bool) {
		r, _, err := sessionLine(value, remote)
		if err == nil && (!numericID(r.SessionID, '$') || r.Created < 0 || r.Windows < 0 || r.Clients < 0 || clean(r.Name) != r.Name) {
			t.Fatalf("invalid accepted record: %+v", r)
		}
	})
}
