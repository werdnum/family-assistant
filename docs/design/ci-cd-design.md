# CI/CD Design

This document outlines the CI/CD strategy for the `family-assistant` project. It covers the current
implementation using ConcourseCI and a potential future alternative using GitOps.

## Current CI/CD Flow (Concourse-based)

The current implementation uses a hybrid approach where GitHub Actions is responsible for building
the container image, and ConcourseCI is responsible for deploying it to the internal Kubernetes
cluster.

The flow is as follows:

1. **Image Build:** A GitHub Actions workflow (`.github/workflows/build-containers.yml`) builds the
   `family-assistant` Docker image on every push to the `main` branch and pushes it to
   `ghcr.io/werdnum/family-assistant` with the `main` tag.

2. **Deployment Trigger:** The GitHub Actions workflow then sends a `POST` request to a ConcourseCI
   webhook. This webhook is exposed externally via a Tailscale funnel ingress.

3. **Concourse Pipeline:** This webhook triggers the `deploy` job in the `family-assistant`
   Concourse pipeline.

4. **Deployment:** The Concourse pipeline fetches the latest image digest from `ghcr.io` and uses
   the `k8s-resource` to update the `family-assistant` deployment in the `ml-bot` Kubernetes
   namespace with the new image.

### Advantages of this approach

- It reuses the existing ConcourseCI infrastructure.
- It allows building images using GitHub Actions' infrastructure.

### Disadvantages

- It requires exposing a webhook from the internal network.

## Alternative: GitOps Flow (Future Direction)

A GitOps-based approach using a tool like [ArgoCD](https://argo-cd.readthedocs.io/en/stable/) or
[FluxCD](https://fluxcd.io/) would offer a more secure and declarative way to manage deployments.

The flow would be as follows:

1. **Image Build:** The GitHub Actions workflow builds and pushes the Docker image to `ghcr.io` as
   it does now.

2. **Configuration Update:** Instead of calling a webhook, the GitHub Actions workflow updates a
   Kubernetes manifest file (e.g., `deploy/deployment.yaml`) in a dedicated Git repository (a
   "config repo", or within the application repo itself). The change would be to update the `image`
   field to the new image digest.

3. **Automated Deployment:** A GitOps agent (ArgoCD or FluxCD) running inside the Kubernetes cluster
   continuously monitors the config repository.

4. **Synchronization:** When the agent detects a change in the Git repository (i.e., the new commit
   with the updated image tag), it automatically applies the change to the cluster, causing
   Kubernetes to pull the new image and update the deployment.

### Advantages of the GitOps Approach

- **Enhanced Security:** The cluster *pulls* changes from a trusted Git source. There is no need to
  expose any part of the cluster or CI system (like Concourse webhooks) to the internet. This is
  ideal for clusters on internal networks.
- **Declarative State:** The Git repository becomes the single source of truth for the desired state
  of the application. Everything is version-controlled and auditable.
- **Consistency and Reliability:** Automated synchronization ensures that the state of the cluster
  always matches the state defined in Git.
- **Developer Experience:** Developers can use familiar Git workflows to manage deployments.

### Considerations

The main consideration for this approach is the one-time setup and configuration of the GitOps tool
(ArgoCD or FluxCD) within the Kubernetes cluster.

This approach is a widely adopted best practice for Kubernetes application delivery and could be a
valuable improvement for the `family-assistant` project in the future.
