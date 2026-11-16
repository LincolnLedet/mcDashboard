#!/bin/bash
# Copy mcdash's BlueMap scripts into BlueMap's web root and bump their ?v= so browsers drop cached copies
# (BlueMap serves scripts with a 1-day cache, but always re-checks settings.json).
set -e
SRC=$(dirname "$0"); WEB=~/minecraft/server/bluemap/web; CONF=~/minecraft/server/plugins/BlueMap/webapp.conf
# refuse to publish a script that doesn't parse (a broken script would break the territory map for everyone)
for f in autofocus.js territories.js; do
  podman run --rm -v "$(realpath "$SRC/$f")":/check.js:ro,z docker.io/library/node:22-alpine node --check /check.js \
    || { echo "syntax error in $f, nothing deployed" >&2; exit 1; }
done
V=$(date +%s)
cp "$SRC/autofocus.js" "$SRC/territories.js" "$WEB/js/"
sed -i -E "s#\"js/(autofocus|territories)\.js(\?v=[0-9]+)?\"#\"js/\1.js?v=$V\"#" "$CONF"
python3 - "$WEB/settings.json" "$V" <<'PY'
import json, re, sys
p, v = sys.argv[1], sys.argv[2]
d = json.load(open(p))
names = ["js/autofocus.js", "js/territories.js"]
d["scripts"] = [s for s in d["scripts"] if re.sub(r"\?v=\d+$", "", s) not in names] + [f"{n}?v={v}" for n in names]
json.dump(d, open(p, "w"))
print("scripts:", d["scripts"])
PY
