##!/bin/bash

# Directory containing your .so files
PLUGIN_DIR="$(pwd)/build/8.5/caddy-plugins"

# Temporary file to store aggregated data
DATA_FILE=$(mktemp)

echo "Scanning plugins in $PLUGIN_DIR..."

# 1. Collect all dependency lines from all .so files
find "$PLUGIN_DIR" -name "*.so" -print0 | while IFS= read -r -d '' file; do
    # Extract lines starting with 'dep', clean up whitespace, and append filename
    go version -m "$file" | grep $'\tdep\t' | sed "s/$/\t$(basename "$file")/" >> "$DATA_FILE"
done

# 2. Process the data to find mismatches
echo "------------------------------------------------"
echo "Conflicting Dependencies Found:"
echo "------------------------------------------------"

awk -F'\t' '
{
    pkg = $3;
    ver = $4;
    hash = $5;
    file = $6;
    full_ver = ver " " hash;

    # Track unique versions found for this package
    if (!( (pkg, full_ver) in seen )) {
        seen[pkg, full_ver] = 1;
        count[pkg]++;
        # Append filename to the specific version string
        version_map[pkg, count[pkg]] = full_ver;
        file_map[pkg, count[pkg]] = file;
    } else {
        # If we have seen this version before, just append the filename to it
        for (i = 1; i <= count[pkg]; i++) {
            if (version_map[pkg, i] == full_ver) {
                file_map[pkg, i] = file_map[pkg, i] " " file;
                break;
            }
        }
    }
}
END {
    found = 0;
    for (pkg in count) {
        # ONLY print if more than one version was detected
        if (count[pkg] > 1) {
            found = 1;
            print "Package: " pkg;
            for (i = 1; i <= count[pkg]; i++) {
                print "  -> Version: " version_map[pkg, i] " (used in: " file_map[pkg, i] ")";
            }
            print "";
        }
    }
    if (found == 0) {
        print "No version mismatches found across .so files.";
    }
}
' "$DATA_FILE"

rm "$DATA_FILE"
