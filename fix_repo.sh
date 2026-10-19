#!/bin/bash
# Run from the repository root, AFTER unzipping this package over it.
# Tidies git: stops tracking the junk, removes the files that are gone, and clears the conflicted data file.
set -e
[ -d .git ] && [ -d ufd/ufd_app ] || { echo "Run this from the repo root (the folder with .git and ufd/)."; exit 1; }

echo "1/4  Stop tracking the harvest folder and the stray nested copy (the files stay on disk where noted)"
git rm -r --cached -q --ignore-unmatch pitchapi_raw pitchapi-only-files
rm -rf pitchapi-only-files                      # a mistaken copy of this package's files; pitchapi_raw/ is kept on disk

echo "2/4  Remove apifootball, its tests, the old probe scripts and the corrupt history copy"
git rm -q -f --ignore-unmatch ufd/ufd_app/apifootball.py ufd/ufd_app/tests.py probe.py probe_report.json \
    club_extras.suggested.json ufd/ufd_app/data/pl_history.corrupt.json
rm -f ufd/ufd_app/apifootball.py ufd/ufd_app/tests.py probe.py probe_report.json club_extras.suggested.json \
    ufd/ufd_app/data/pl_history.corrupt.json

echo "3/4  pl_teams.json: back to the last good committed copy (no conflict markers); the next build refreshes it"
git checkout HEAD -- ufd/ufd_app/data/pl_teams.json

echo "4/4  Check the data files are sound"
python ufd/manage.py check_data

echo
echo "Done. Review with:  git status"
echo "Then:  git add -A && git commit -m 'PitchAPI only: remove apifootball, repair history, guard the data files'"
echo "Stashes are NOT touched. Once you have committed, see the notes about 'git stash drop'."
