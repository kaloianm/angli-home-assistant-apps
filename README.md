# Home Assistant apps

This repository contains reusable AppDaemon applications used by my Home Assistant setup. It is linked as a submodule under the apps/ folder.

## Purpose

- Keep automation app code public, shareable, and versioned independently.
- Keep installation-specific Home Assistant configuration private (entity mapping, KNX layout, dashboards, secrets).
- Provide a clean place to develop and test AppDaemon apps that can be reused across projects.

## Structure

- `extractor_fan_control/`: Extractor fan automation app with:
  - pure logic module
  - configuration parsing/validation
  - AppDaemon integration layer
  - unit tests

## How It Is Used

This repository is consumed as a git submodule in the private Home Assistant config repo under `apps/public_apps`.
The private repo keeps `apps/apps.yaml`, where each app is wired to concrete Home Assistant entities for that installation.
