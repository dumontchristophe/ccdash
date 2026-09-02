# No runtime dependency: Python stdlib and native ES modules only

ccdash stores the prompts, commands and paths a developer ran, so what it depends
on is what it can be trusted with: it runs on the Python standard library and
browser-native ES modules alone — no pip, npm, manifest, lockfile or bundler. The
one build step compiles the Tailwind stylesheet, whose output is committed, so
cloning the repo and running `python3 -m ccdash` needs nothing fetched or built.
