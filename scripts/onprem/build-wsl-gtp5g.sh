#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

readonly SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
readonly DEFAULT_BUILD_ROOT="/opt/5gcityverse"
readonly WSL_KERNEL_TAG="linux-msft-wsl-5.15.153.1"
readonly GTP5G_TAG="v0.10.2"
readonly REPO_ROOT="/mnt/d/projects/5GCityVerse"
readonly PATCH_FILE="${REPO_ROOT}/infrastructure/shared/node-patches/gtp5g-zero-rate-qer.patch"

build_root="${BUILD_ROOT:-${DEFAULT_BUILD_ROOT}}"
jobs="${JOBS:-8}"

usage() {
  cat >&2 <<EOF
Usage: ${SCRIPT_NAME} COMMAND

Commands:
  preflight              Print WSL kernel/module readiness.
  install-deps           Install Ubuntu build dependencies. Run as root in WSL.
  prepare-sources        Clone/update matching WSL kernel and gtp5g sources.
  build-kernel-symbols   Build matching WSL kernel symbols with max JOBS.
  build-gtp5g            Apply repo patch and build gtp5g.ko.
  load-gtp5g             Load the built module only when vermagic matches.
  verify-nobtf-artifact   Check the fallback module sections without loading it.
  all                    Run prepare-sources, build-kernel-symbols, build-gtp5g.

Environment:
  BUILD_ROOT             Default: ${DEFAULT_BUILD_ROOT}
  JOBS                   Default: 8
EOF
}

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

artifact_base() {
  printf '%s/artifacts/gtp5g-%s-%s-patched' "${build_root}" "$(uname -r)" "${GTP5G_TAG}"
}

require_wsl_kernel() {
  local release
  release="$(uname -r)"
  [[ "${release}" == "5.15.153.1-microsoft-standard-WSL2" ]] || die "unexpected kernel release: ${release}"
}

preflight() {
  require_wsl_kernel
  log "kernel: $(uname -a)"
  zgrep -E 'CONFIG_(MODULES|MODVERSIONS|MODULE_SIG|IP_SCTP|TUN|NET_NS|NET_UDP_TUNNEL)=|# CONFIG_GTP' /proc/config.gz || true
  zgrep -E 'CONFIG_(NF_CT_PROTO_SCTP|NETFILTER_XT_MATCH_SCTP)=' /proc/config.gz || true
  grep -E 'SCTP|GTP' /proc/net/protocols || true
  modinfo sctp | sed -n '1,8p' || true
  modinfo gtp 2>&1 || true
  ls -ld "/lib/modules/$(uname -r)/build" "/lib/modules/$(uname -r)/source" 2>&1 || true
}

install_deps() {
  [[ "${EUID}" -eq 0 ]] || die "install-deps must run as root; use: wsl -d Ubuntu -u root -- bash ${SCRIPT_NAME} install-deps"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    autoconf bc bison build-essential ca-certificates cpio dwarves flex git \
    libelf-dev libmnl-dev libssl-dev libtool libyaml-dev pkg-config rsync
}

git_repo() {
  local repo="$1"
  shift
  git -c "safe.directory=${repo}" -C "${repo}" "$@"
}

prepare_sources() {
  require_wsl_kernel
  mkdir -p "${build_root}/src" "${build_root}/artifacts" "${build_root}/logs"
  if [[ ! -d "${build_root}/src/WSL2-Linux-Kernel/.git" ]]; then
    git clone --depth 1 --branch "${WSL_KERNEL_TAG}" \
      https://github.com/microsoft/WSL2-Linux-Kernel.git \
      "${build_root}/src/WSL2-Linux-Kernel"
  else
    git_repo "${build_root}/src/WSL2-Linux-Kernel" fetch --depth 1 origin "tag" "${WSL_KERNEL_TAG}"
    git_repo "${build_root}/src/WSL2-Linux-Kernel" checkout -f "${WSL_KERNEL_TAG}"
  fi
  if [[ ! -d "${build_root}/src/gtp5g/.git" ]]; then
    git clone --depth 1 --branch "${GTP5G_TAG}" \
      https://github.com/free5gc/gtp5g.git \
      "${build_root}/src/gtp5g"
  else
    git_repo "${build_root}/src/gtp5g" fetch --depth 1 origin "tag" "${GTP5G_TAG}"
    git_repo "${build_root}/src/gtp5g" checkout -f "${GTP5G_TAG}"
  fi
  zcat /proc/config.gz > "${build_root}/artifacts/live-config-$(uname -r).config"
  cp "${build_root}/artifacts/live-config-$(uname -r).config" \
    "${build_root}/src/WSL2-Linux-Kernel/Microsoft/config-wsl"
}

build_kernel_symbols() {
  require_wsl_kernel
  # The running WSL kernel omits the SCM dirty-tree suffix. Kernel make adds
  # '+' when LOCALVERSION is unset, even with CONFIG_LOCALVERSION_AUTO=n.
  # An explicitly empty value preserves the exact running release/vermagic.
  export LOCALVERSION=""
  local kernel_src="${build_root}/src/WSL2-Linux-Kernel"
  [[ -d "${kernel_src}" ]] || die "missing kernel source: ${kernel_src}"
  make -C "${kernel_src}" KCONFIG_CONFIG=Microsoft/config-wsl olddefconfig
  make -C "${kernel_src}" KCONFIG_CONFIG=Microsoft/config-wsl prepare
  local release
  release="$(cat "${kernel_src}/include/config/kernel.release")"
  [[ "${release}" == "$(uname -r)" ]] || die "kernelrelease mismatch: source=${release} running=$(uname -r)"
  make -C "${kernel_src}" -j"${jobs}" KCONFIG_CONFIG=Microsoft/config-wsl 2>&1 | tee "${build_root}/logs/wsl-kernel-build.log"
  [[ -s "${kernel_src}/Module.symvers" ]] || die "kernel build did not produce Module.symvers"
  cp "${kernel_src}/Module.symvers" "${build_root}/artifacts/Module.symvers-$(uname -r)"
}

build_gtp5g() {
  require_wsl_kernel
  export LOCALVERSION=""
  local kernel_src="${build_root}/src/WSL2-Linux-Kernel"
  local gtp5g_src="${build_root}/src/gtp5g"
  [[ -s "${kernel_src}/Module.symvers" ]] || die "missing Module.symvers; run build-kernel-symbols first"
  [[ -f "${PATCH_FILE}" ]] || die "missing patch: ${PATCH_FILE}"
  git_repo "${gtp5g_src}" checkout -f "${GTP5G_TAG}"
  git_repo "${gtp5g_src}" clean -fdx
  git_repo "${gtp5g_src}" apply --check "${PATCH_FILE}"
  git_repo "${gtp5g_src}" apply "${PATCH_FILE}"
  make -C "${gtp5g_src}" clean KDIR="${kernel_src}" || true
  make -C "${gtp5g_src}" KDIR="${kernel_src}" 2>&1 | tee "${build_root}/logs/gtp5g-build.log"
  [[ -s "${gtp5g_src}/gtp5g.ko" ]] || die "gtp5g.ko was not produced"
  local artifact_prefix
  artifact_prefix="$(artifact_base)"
  cp "${gtp5g_src}/gtp5g.ko" "${artifact_prefix}.ko"
  cp "${gtp5g_src}/gtp5g.ko" "${artifact_prefix}-nobtf.ko"
  objcopy --remove-section .BTF --remove-section .BTF.ext \
    "${artifact_prefix}-nobtf.ko"
  log "built original artifact: ${artifact_prefix}.ko"
  modinfo "${artifact_prefix}.ko"
  log "built no-BTF fallback artifact: ${artifact_prefix}-nobtf.ko"
  modinfo "${artifact_prefix}-nobtf.ko"
  readelf -S "${artifact_prefix}-nobtf.ko" | grep -q '__versions' || die "no-BTF artifact lost __versions"
  if readelf -S "${artifact_prefix}-nobtf.ko" | grep -q 'BTF'; then
    die "no-BTF artifact still contains BTF sections"
  fi
}

verify_nobtf_sections() {
  local module_path="${1:?module path required}"
  local sections
  sections="$(readelf -SW "${module_path}")" || die "cannot read module sections: ${module_path}"
  grep -Eq '[[:space:]]__versions[[:space:]]' <<<"${sections}" || die "no-BTF artifact lost __versions"
  if grep -Eq '[[:space:]]\.BTF([[:space:]]|$)' <<<"${sections}"; then
    die "no-BTF artifact still contains BTF sections"
  fi
}

verify_nobtf_artifact() {
  require_wsl_kernel
  local module_path="$(artifact_base)-nobtf.ko"
  [[ -s "${module_path}" ]] || die "missing no-BTF module: ${module_path}"
  [[ "$(modinfo -F vermagic "${module_path}")" == "$(uname -r)"* ]] || die "no-BTF vermagic mismatch"
  verify_nobtf_sections "${module_path}"
  log "verified no-BTF artifact: ${module_path}"
}

load_gtp5g() {
  require_wsl_kernel
  local artifact_prefix
  artifact_prefix="$(artifact_base)"
  local module_path="${artifact_prefix}.ko"
  local nobtf_module_path="${artifact_prefix}-nobtf.ko"
  [[ -s "${module_path}" ]] || die "missing built module: ${module_path}"
  local vermagic
  vermagic="$(modinfo -F vermagic "${module_path}")"
  [[ "${vermagic}" == "$(uname -r)"* ]] || die "vermagic mismatch: ${vermagic}"
  if grep -q '^gtp5g ' /proc/modules; then
    log "gtp5g already loaded; leaving existing module untouched"
    cat /proc/gtp5g/qos 2>/dev/null || true
    return 0
  fi
  [[ "${EUID}" -eq 0 ]] || die "load-gtp5g must run as root"
  modprobe udp_tunnel
  local loaded_module_path="${module_path}"
  if ! insmod "${module_path}"; then
    log "plain artifact failed to load; trying no-BTF artifact without changing vermagic or __versions"
    dmesg | tail -n 8 >&2 || true
    verify_nobtf_artifact
    insmod "${nobtf_module_path}"
    loaded_module_path="${nobtf_module_path}"
  fi
  if [[ -w /proc/gtp5g/qos ]]; then
    echo 1 > /proc/gtp5g/qos
  fi
  log "loaded module artifact: ${loaded_module_path}"
  grep -E '^(gtp5g|udp_tunnel) ' /proc/modules
  cat /proc/gtp5g/qos 2>/dev/null || true
}

main() {
  local command="${1:-}"
  case "${command}" in
    preflight) preflight ;;
    install-deps) install_deps ;;
    prepare-sources) prepare_sources ;;
    build-kernel-symbols) build_kernel_symbols ;;
    build-gtp5g) build_gtp5g ;;
    verify-nobtf-artifact) verify_nobtf_artifact ;;
    load-gtp5g) load_gtp5g ;;
    all)
      prepare_sources
      build_kernel_symbols
      build_gtp5g
      ;;
    -h|--help|help) usage ;;
    *) usage; exit 2 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
