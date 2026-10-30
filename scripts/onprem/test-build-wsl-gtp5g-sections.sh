#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/build-wsl-gtp5g.sh"

fixture_dir="$(mktemp -d)"
trap 'rm -rf -- "${fixture_dir}"' EXIT
printf 'fixture\n' >"${fixture_dir}/payload"
cp /bin/true "${fixture_dir}/missing.ko"
objcopy --add-section "__versions=${fixture_dir}/payload" \
  "${fixture_dir}/missing.ko" "${fixture_dir}/valid.ko"
objcopy --add-section ".BTF=${fixture_dir}/payload" \
  "${fixture_dir}/valid.ko" "${fixture_dir}/btf.ko"

verify_nobtf_sections "${fixture_dir}/valid.ko"
if (verify_nobtf_sections "${fixture_dir}/missing.ko") 2>/dev/null; then
  printf 'module without __versions was accepted\n' >&2
  exit 1
fi
if (verify_nobtf_sections "${fixture_dir}/btf.ko") 2>/dev/null; then
  printf 'module with .BTF was accepted\n' >&2
  exit 1
fi
printf 'gtp5g section validation passed\n'
