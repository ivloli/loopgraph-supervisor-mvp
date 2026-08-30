#!/usr/bin/env bash
set -euo pipefail

# Pin these URLs to reviewed commits before using them in a production profile.
dsh plugin --profile web add "github:omdsh-dev/dsh_workflow#main"
dsh plugin --profile web add "github:PerryLink/dsh-doublecheck#v0.8.0"
dsh plugin --profile web add "github:PerryLink/dsh-background-agents#main"
dsh plugin --profile web add "dsh-flowglass"
dsh plugin --profile web add "@necokeine/dsh-git"
dsh plugin --profile web add "./packages/dsh-loopgraph-supervisor"

# dsh-background-agents requires a continuable subagent provider in the
# profile. Configure that provider in the profile composition before starting.
