@branch-resolution
Feature: Strict branch source resolution
  Branch inputs stay in the namespace that the user selected.

  @BR-001
  Scenario: A local branch ignores its upstream when resolving a comparison
    Given a local branch with an upstream tracking branch
    When I run a local-base comparison for "feature"
    Then the local branch comparison succeeds without a fetch

  @BR-002
  Scenario: A missing local branch does not fall back to a remote branch
    Given a remote-only branch named "release"
    When I run a local-base comparison for "release"
    Then the local branch comparison fails directly

  @BR-003
  Scenario: An explicit remote branch uses the remote namespace
    Given the same branch exists locally and on the remote as "release"
    When I run a remote-base comparison for "origin/release"
    Then the remote branch comparison succeeds after fetching

  @BR-004
  Scenario: A deleted remote branch is not satisfied by a cached tracking ref
    Given a cached remote branch is deleted as "release"
    When I run a remote-base comparison for "origin/release"
    Then the remote branch comparison fails directly

  @BR-005
  Scenario: A remote branch requires network verification
    Given a local repository with a remote default branch
    When I run a remote-base comparison for "origin/main" with no fetch
    Then the remote comparison reports the no-fetch conflict

  @BR-006
  Scenario: Tags commits HEAD and slash-named local branches remain supported
    Given tag, commit, HEAD, and a slash-named local branch inputs
    When I run all explicit ref comparisons
    Then all explicit ref comparisons succeed
