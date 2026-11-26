# ANAS Evaluation Manager for Proxmox VE 9.3

This independent toolkit installs the reviewed [ANAS 0.4.6 release](https://github.com/ccebelenski/anas/releases/tag/v0.4.6), tracks its APT dependencies, and checks daily for newer upstream releases. It downloads ANAS directly from the upstream project and verifies the release archive before running it.

## Use

Run from a checkout of this repository on a Proxmox VE 9.3.x node as root. Use SSH or the local console, since ANAS updates the Proxmox web UI integration and can interrupt a Proxmox web shell.

| Command | Operation |
|---|---|
| `./anasctl install --plan` | Preview the APT dependency changes. |
| `./anasctl install` | Install and verify ANAS, its dependencies, and the daily update timer. |
| `./anasctl status` | Show the installed version, timer, notification route, and pending operation. |
| `./anasctl uninstall` | Remove ANAS and the eligible dependencies it introduced. |
| `./anasctl recover` | Resume rollback or finalization after a failed or interrupted operation. |

The manager runs per node. Each node owns its package ledger, timer, and fallback notification templates. Proxmox still applies its shared Datacenter notification configuration and any administrator-provided cluster template overrides. The release pin covers the installer and dependency set reviewed together. Newer releases are reported for review before they become installation targets. [Proxmox VE 9.3 release](https://forum.proxmox.com/threads/proxmox-virtual-environment-9-3-released.187799/)

Uninstall checks for active ANAS jobs, backs up ANAS schedule definitions, and caches the exact package versions needed for rollback. The manager preserves storage data, native storage configuration, pre-existing packages, packages marked manual, and dependencies needed by other installed software. It also retains detected storage services in use. Mark a package manual if an independent script or workload relies on it, since package metadata may omit that relationship. Avoid concurrent storage jobs and package maintenance while installing or uninstalling.

## Notifications

The timer checks GitHub at **07:00 in the node's local time zone** and catches up after downtime. Proxmox routes the `anas-update-available` event at severity `info` using the existing matchers and enabled targets. The event's `hostname` field contains the Proxmox node name. A working default matcher or an ANAS-specific matcher can both route notices. [Proxmox notification matchers](https://github.com/proxmox/pve-docs/blob/master/notifications.adoc#notification-matchers)

Run `./anasctl notifications` for a read-only route check. Missing or disabled routes, missing mail recipients, and an inactive notification worker produce local warnings during installation and in the journal during scheduled checks. Release detection continues. To enable delivery, open **Datacenter → Notifications**, enable or add a target and a suitable matcher, and test the target from Proxmox. Proxmox evaluates calendar and regex filters during delivery.

- `./anasctl check` checks GitHub immediately and prints the result.
- `./anasctl check --notify` submits a notice for a newer release that the manager has not previously submitted.
- `./anasctl check --notify --resend` resubmits the current update notice after a delivery problem is fixed.

```sh
journalctl -u anas-update-check.service -u pve-notify-worker.service --no-pager
```

## Recovery

The manager serializes its operations and keeps a journal under `/var/lib/anas-manager`. A failed installation triggers cleanup. A failed uninstall before commit triggers restoration of the app, timers, and removed dependency versions using the cached packages. Recovery can resume with `./anasctl recover`. Review the session's `lifecycle.log`, package history, and any schedule backups using the path from `./anasctl status`. A damaged dpkg database, unavailable service, or external changes can block recovery. Side effects from package maintainer scripts outside the recorded transaction may require separate repair.

## Development

Run the standard-library tests from the repository root. They use temporary directories and simulated lifecycle operations, so the test suite can also run outside Proxmox.

```sh
python3 -m unittest discover -v
```
