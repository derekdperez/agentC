# Dotfiles Backup Tool: Design Document

## 1. Overview

### Purpose
A lightweight tool to automate backing up dotfiles to a Git repository, ensuring version control and easy restoration across systems.

### Goals
- Simplify dotfiles management for developers.
- Ensure reliable backup and restoration.
- Handle edge cases (symlinks, sensitive data, large files).
- Provide a CLI interface for ease of use.

### Scope
- Backup dotfiles from `$HOME` to a local or remote Git repository.
- Support selective inclusion/exclusion of files.
- Handle edge cases (e.g., symlinks, permissions).
- Provide restore functionality.

## 2. Requirements

### Functional Requirements
1. **Backup**: Copy dotfiles from `$HOME` to a Git repository.
2. **Restore**: Copy dotfiles from the repository back to `$HOME`.
3. **Configuration**: Allow users to specify included/excluded files via a config file.
4. **Conflict Handling**: Detect and resolve conflicts during backup/restore.
5. **Git Integration**: Automate Git commits, pushes, and pulls.

### Non-Functional Requirements
1. **Performance**: Minimize runtime for typical dotfile sets.
2. **Security**: Avoid exposing sensitive data (e.g., credentials, tokens).
3. **Portability**: Work on Linux, macOS, and Windows (WSL).
4. **Reliability**: Gracefully handle errors and edge cases.

## 3. Design

### High-Level Architecture
```
┌─────────────┐       ┌─────────────┐       ┌─────────────┐
│   $HOME    │       │ Config File │       │ Git Repo    │
└──────┬──────┘       └──────┬──────┘       └──────┬──────┘
       │                   │                   │
       ▼                   ▼                   ▼
┌───────────────────────────────────────────────────┐
│           Dotfiles Backup Tool                     │
│ ┌─────────────┐   ┌─────────────┐   ┌─────────┐ │
│ │ Backup      │   │ Restore     │   │ Git     │ │
│ │ Manager     │◄──►│ Manager     │◄──►│ Manager │ │
│ └─────────────┘   └─────────────┘   └─────────┘ │
└───────────────────────────────────────────────────┘
```

### Key Components
1. **Backup Manager**: Handles file selection, copying, and conflict detection.
2. **Restore Manager**: Handles file restoration and conflict resolution.
3. **Git Manager**: Manages Git operations (commits, pushes, pulls).
4. **Config Parser**: Reads and validates user configuration.

### Workflow

#### Backup
1. Read config file to determine included/excluded files.
2. Copy dotfiles from `$HOME` to a staging directory.
3. Detect conflicts (e.g., existing files in the repo).
4. Commit and push changes to Git.

#### Restore
1. Pull latest changes from Git.
2. Copy files from the repo to `$HOME`.
3. Resolve conflicts (e.g., existing files in `$HOME`).

## 4. Implementation Details

### File Selection
- Use a `.dotfiles_backup.toml` config file in `$HOME` to specify:
  - Included files/directories (glob patterns).
  - Excluded files/directories (e.g., `*.secret`, `*.tmp`).
- Default exclusions: `*.git*`, `*.ssh/id_*`, `*.cache`, `*.tmp`.

### Git Interaction
- Initialize a Git repo in `~/.dotfiles` (configurable).
- Automate commits with messages like `Backup: <timestamp>`.
- Support remote repositories (e.g., GitHub, GitLab).

### Conflict Handling
- **Backup**: Skip, overwrite, or prompt for files in the repo (user-configurable).
- **Restore**: Skip, overwrite, or merge files in `$HOME` (user-configurable).

### Technical Considerations
- **Symlinks**: Preserve or dereference (configurable).
- **Permissions**: Preserve file permissions (`chmod`).
- **Large Files**: Warn if files exceed a configurable size limit (default: 1MB).
- **Case-Insensitive FS**: Normalize paths (e.g., macOS).

## 5. Usage

### CLI Commands
```sh
# Initialize a new backup
dotfiles init --repo ~/.dotfiles --remote git@github.com:user/dotfiles.git

# Backup dotfiles
dotfiles backup

# Restore dotfiles
dotfiles restore

# Add/remove files
dotfiles add ~/.bashrc
dotfiles remove ~/.bashrc
```

### Configuration File
```toml
# ~/.dotfiles_backup.toml
[backup]
include = [
    "~/.bashrc",
    "~/.config/**"
]
exclude = [
    "~/.ssh/id_*",
    "~/.cache/**"
]

handle_symlinks = "preserve"  # or "dereference"

[git]
repo = "~/.dotfiles"
remote = "git@github.com:user/dotfiles.git"
auto_push = true

[limits]
max_file_size = "1MB"  # e.g., "1MB", "500KB"
```

## 6. Edge Cases

| Edge Case               | Resolution                                      |
|-------------------------|------------------------------------------------|
| Symlinks                | Preserve or dereference (configurable).         |
| Sensitive Data          | Exclude by default (e.g., `*.secret`, `*.key`). |
| Large Files             | Skip or warn if exceeding size limit.           |
| Permission Denied       | Skip or prompt for `sudo`.                      |
| Git Conflicts           | Skip, overwrite, or merge (configurable).       |
| Case-Insensitive FS     | Normalize paths (e.g., macOS).                  |

## 7. Testing

### Unit Tests
- Test file selection, conflict detection, and Git operations.
- Mock filesystem and Git responses.

### Integration Tests
- Test backup/restore workflows with real dotfiles.
- Verify edge cases (symlinks, permissions, conflicts).

### End-to-End Tests
- Run the tool on a clean VM and verify backup/restore.

## 8. Future Extensions
1. **Encryption**: Encrypt sensitive files before backup.
2. **Automation**: Schedule backups via cron/systemd.
3. **Cloud Sync**: Support syncing to cloud storage (e.g., S3, Dropbox).
4. **GUI**: Build a TUI/GUI for non-CLI users.
5. **Plugins**: Support hooks for custom pre/post-processing.

## 9. Appendix

### Default Exclusions
```
*.git*
*.ssh/id_*
*.cache
*.tmp
*.secret
*.key
*.swp
*.DS_Store
*.log
```

### Size Limits
- Default: 1MB (configurable in `.dotfiles_backup.toml`).
- Files exceeding the limit are skipped by default.

### Symlink Handling
- **preserve**: Keep symlinks as-is.
- **dereference**: Copy the symlink target instead.