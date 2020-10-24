## System Helm Chart Repository

__The `System Helm Chart Repository` has been designed for internal usage only. That means regular users must not reuse it for any purpose.__

System Helm charts are stored in the following location: `roles/helm_charts/files/system`.

All charts should be added there unarchived in separate directories (not tarballs).

Repository role is responsible for retrieving system Helm charts. It copies all the directories, then archives them (`.tgz`), generates helm index file and serves these archives in apache HTTP server.


## Installing Helm charts from the system Helm repository

Installation of a particular system Helm chart is performed in separate role. Each caller role that needs to install system Helm chart has to trigger the "system Helm chart installation task" (let's call it `SHCIT`): `roles/helm/tasks/install-system-chart.yml`.

`SHCIT` is responsible for installing already existing system charts from the system Helm repository only.

It is possible to overwrite chart values within the specification config. `SHCIT` expects 4 parameters to be passed to the `roles/helm/tasks/install-system-chart.yml`:

```yaml
    disable_helm_chart:
    helm_chart_name:
    helm_chart_values:
    helm_release_name:
```

Example usage:

```yaml
---
- name: Set Helm chart disable flag from custom configuration
  set_fact:
    _disable_helm_chart: "{{ specification.disable_helm_chart }}"
  when: specification.disable_helm_chart is defined

- name: Set Helm chart name from custom configuration
  set_fact:
    _helm_chart_name: "{{ specification.helm_chart_name }}"
  when: specification.helm_chart_name is defined

- name: Mychart
  include_role:
    name: helm
    tasks_from: install-system-chart
  vars:
    disable_helm_chart: "{{ _disable_helm_chart }}"
    helm_chart_name: "{{ _helm_chart_name }}"
    helm_release_name: "{{ specification.helm_release_name }}"
    helm_chart_values:  "{{ specification.helm_chart_values }}"
```

Example config:

```yaml
kind: configuration/helm-mychart
title: "Helm mychart"
name: default
specification:
  helm_chart_name: mychart
  helm_release_name: myrelease
  disable_helm_chart: false
  helm_chart_values:
    service:
      port: 8080
    nameOverride: mychart_custom_name
```
