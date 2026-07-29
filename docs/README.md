# TaleTrace developer documentation

This directory describes TaleTrace as a set of software boundaries. It is
intended for developers adding implementations behind the existing contracts.
It documents **responsibilities, inputs, outputs, and dependencies** only;
algorithms, provider configuration, and implementation procedures are outside
this document.

## Documentation map

- [System architecture](architecture.md) — module boundaries and dependency direction
- [Module contracts](module-contracts.md) — inputs, outputs, dependencies, and reserved integrations
- [Database schema](database-schema.md) — persistence model vocabulary and relationships

## Current contract status

The domain, OCR, AI Engine, and Gesture Engine contracts are provider-neutral.
Placeholder routes and capability shells may return pending responses. A
contract describes what a module accepts and returns; it does not imply that a
capability is implemented.
