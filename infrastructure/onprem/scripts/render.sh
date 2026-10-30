#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s --upstream PATH --output NEW_DIRECTORY [--values FILE] [--release NAME] [--namespace NAME]\n' "${0##*/}"
}

chart_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
upstream='' output='' release=cityverse namespace=free5gc-onprem
overlays=()
while (( $# )); do
  case "$1" in
    --upstream|--output|--values|--release|--namespace)
      (( $# >= 2 )) || { usage >&2; exit 2; }
      case "$1" in
        --upstream) upstream=$2 ;;
        --output) output=$2 ;;
        --values) overlays+=(-f "$(realpath -- "$2")") ;;
        --release) release=$2 ;;
        --namespace) namespace=$2 ;;
      esac
      shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n $upstream && -n $output && ! -e $output ]] || { usage >&2; exit 2; }
[[ $namespace =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#namespace} -le 63 ]] || exit 2
for executable in helm git kubectl realpath cmp; do
  command -v "$executable" >/dev/null || { printf 'Missing tool: %s\n' "$executable" >&2; exit 1; }
done
upstream=$(realpath -- "$upstream")
revision=$(awk '$1 == "revision:" {print $2}' "$chart_root/upstream.lock.yaml")
[[ $revision =~ ^[a-f0-9]{40}$ ]] || { printf 'Invalid upstream lock\n' >&2; exit 1; }
[[ $(git -C "$upstream" rev-parse HEAD) == "$revision" ]] || { printf 'Upstream revision mismatch\n' >&2; exit 1; }
[[ -z $(git -C "$upstream" status --porcelain --untracked-files=all) ]] || { printf 'Upstream checkout must be clean\n' >&2; exit 1; }

stage=$(mktemp -d -t cityverse-helm.XXXXXXXX)
cleanup() { [[ $stage == /tmp/cityverse-helm.* && -d $stage ]] && rm -rf -- "$stage"; }
trap cleanup EXIT
mkdir "$stage/chart"
cp "$chart_root/Chart.yaml" "$chart_root/values.yaml" "$chart_root/upstream.lock.yaml" "$chart_root/.helmignore" "$stage/chart/"
cp "$chart_root/values.schema.json" "$stage/chart/"
cp -R "$chart_root/templates" "$stage/chart/"
ln -s "$upstream" "$stage/upstream"
helm dependency update "$stage/chart" --skip-refresh >&2
helm lint "$stage/chart" --strict "${overlays[@]}" >&2
export CITYVERSE_NAMESPACE=$namespace
for pass in first second; do
  helm template "$release" "$stage/chart" --skip-tests -n "$namespace" "${overlays[@]}" \
    --post-renderer "$chart_root/scripts/post-render.sh" > "$stage/$pass.yaml"
done
cmp "$stage/first.yaml" "$stage/second.yaml"
[[ -z $(git -C "$upstream" status --porcelain --untracked-files=all) ]] || { printf 'Upstream changed during packaging\n' >&2; exit 1; }
mkdir -p -- "$output"
cp "$stage/first.yaml" "$output/runtime.yaml"
cp "$stage/chart/Chart.lock" "$output/Chart.lock"
helm package "$stage/chart" --destination "$output" >&2
sha256sum "$output/runtime.yaml"
