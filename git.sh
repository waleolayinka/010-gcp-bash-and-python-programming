#!/usr/bin/env bash
set -Eeuo pipefail

# Git Bash: this script passes /c/... paths to git.exe, which needs MSYS path
# conversion. Undo a leftover `export MSYS_NO_PATHCONV=1` from the calling shell.
unset MSYS_NO_PATHCONV MSYS2_ARG_CONV_EXCL

usage() {
  printf 'Usage: %s [commit message]\n' "$0"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

commit_message="${*:-Update bash programming}"
branch="bash"
folder="bash-programming"
workflows=(
  ".github/workflows/deploy-bash-pipeline.yml"
  ".github/workflows/destroy-bash-resources.yml"
)
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(git -C "$script_dir" rev-parse --show-toplevel)" || {
  printf 'Error: run this script from inside the Git repository.\n' >&2
  exit 1
}
for workflow in "${workflows[@]}"; do
  [[ -f "${repo_root}/${workflow}" ]] || {
    printf 'Error: expected workflow file not found: %s\n' "${repo_root}/${workflow}" >&2
    exit 1
  }
done
source_folder="${repo_root}/${folder}"
[[ -d "$source_folder" ]] || {
  printf 'Error: expected folder not found: %s\n' "$source_folder" >&2
  exit 1
}

figlet_or_echo() {
  if command -v figlet >/dev/null 2>&1; then
    figlet -f slant "$1"
  elif command -v python >/dev/null 2>&1 && python -c "import pyfiglet" >/dev/null 2>&1; then
    python -m pyfiglet -f slant "$1"
  else
    local -A glyphs=(
      [A]=' ### /#   #/#####/#   #/#   #'
      [B]='#### /#   #/#### /#   #/#### '
      [C]=' ####/#    /#    /#    / ####'
      [D]='#### /#   #/#   #/#   #/#### '
      [E]='#####/#    /#### /#    /#####'
      [H]='#   #/#   #/#####/#   #/#   #'
      [N]='#   #/##  #/# # #/#  ##/#   #'
      [O]=' ### /#   #/#   #/#   #/ ### '
      [P]='#### /#   #/#### /#    /#    '
      [R]='#### /#   #/#### /# #  /#  ##'
      [S]=' ####/#    / ### /    #/#### '
      [T]='#####/  #  /  #  /  #  /  #  '
      [Y]='#   #/ # # /  #  /  #  /  #  '
      [-]='     /     /#####/     /     '
      [' ']='     /     /     /     /     '
    )
    local -a rows
    local row character glyph index
    for row in 0 1 2 3 4; do
      for ((index = 0; index < ${#1}; index++)); do
        character="${1:index:1}"
        character="${character^^}"
        if [[ "$character" == " " ]]; then
          glyph="     "
        else
          IFS=/ read -r -a rows <<<"${glyphs[$character]}"
          glyph="${rows[$row]}"
        fi
        printf '%s  ' "$glyph"
      done
      printf '\n'
    done
    printf '\n'
  fi
}

printf '\n'
figlet_or_echo "Branch-Bash"
# The branch may not exist on origin yet (first run in a new repo); if so,
# start it from origin/main and the push below creates it.
if git -C "$repo_root" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
  git -C "$repo_root" fetch origin \
    "+refs/heads/${branch}:refs/remotes/origin/${branch}" \
    "+refs/heads/main:refs/remotes/origin/main"
  base_ref="refs/remotes/origin/${branch}"
else
  printf 'Branch %s not found on origin; creating it from main.\n' "$branch"
  git -C "$repo_root" fetch origin "+refs/heads/main:refs/remotes/origin/main"
  base_ref="refs/remotes/origin/main"
fi

temp_dir="$(mktemp -d)"
target_worktree="${temp_dir}/target"
main_worktree="${temp_dir}/main"
cleanup() {
  git -C "$repo_root" worktree remove --force "$target_worktree" >/dev/null 2>&1 || true
  git -C "$repo_root" worktree remove --force "$main_worktree" >/dev/null 2>&1 || true
  rm -rf "$temp_dir"
  git -C "$repo_root" worktree prune
}
trap cleanup EXIT

git -C "$repo_root" worktree add --detach \
  "$target_worktree" "$base_ref"
rm -rf "${target_worktree}/${folder}"
mkdir -p "${target_worktree}/${folder}"
cp -a "${source_folder}/." "${target_worktree}/${folder}/"
for workflow in "${workflows[@]}"; do
  mkdir -p "${target_worktree}/$(dirname -- "$workflow")"
  cp -a "${repo_root}/${workflow}" "${target_worktree}/${workflow}"
done

git -C "$target_worktree" add -A -- "$folder" "${workflows[@]}"
if git -C "$target_worktree" diff --cached --quiet -- "$folder" "${workflows[@]}"; then
  printf 'No changes in %s or its workflows to commit.\n' "$folder"
else
  git -C "$target_worktree" commit -m "$commit_message" -- "$folder" "${workflows[@]}"
fi

target_commit="$(git -C "$target_worktree" rev-parse HEAD)"
git -C "$repo_root" push origin "${target_commit}:refs/heads/${branch}"
git -C "$repo_root" fetch origin \
  "+refs/heads/${branch}:refs/remotes/origin/${branch}" \
  "+refs/heads/main:refs/remotes/origin/main"

git -C "$repo_root" worktree add --detach \
  "$main_worktree" refs/remotes/origin/main
git -C "$main_worktree" merge --no-edit "$target_commit"
main_commit="$(git -C "$main_worktree" rev-parse HEAD)"
git -C "$repo_root" push origin "${main_commit}:refs/heads/main"
figlet_or_echo "Done"
