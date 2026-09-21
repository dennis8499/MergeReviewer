@quick-review
Feature: Quick review of the current local branch
  The helper compares the local branch with a remote default branch without
  changing the working tree or the user's index.

  @QR-001
  Scenario: A single remote reviews an unpushed local commit
    Given a local repository with a remote default branch
    And the current branch has an unpushed commit
    When I run the quick review without fetching
    Then the result uses the current branch HEAD and the remote default branch
    And the local commit appears in the changed files

  @QR-002
  Scenario: Multiple remotes require an explicit choice
    Given a local repository with two remotes
    When I run the quick review without choosing a remote
    Then the quick review reports the remote candidates
    When I run the quick review with remote "upstream"
    Then the result records remote "upstream"

  @QR-003
  Scenario: Remote-only changes are outside the merge review range
    Given a local repository whose remote default branch has an unrelated commit
    And the current branch has a local commit
    When I run the quick review without fetching
    Then the result contains only the local branch changes

  @QR-004
  Scenario: Working-tree mode includes saved files and preserves the index
    Given a local repository with a remote default branch
    And the working tree has staged, unstaged, deleted, and untracked files
    When I run the quick review with working-tree changes
    Then the result scope is "working-tree"
    And the original working tree and index are unchanged

  @QR-005
  Scenario: Working-tree context remains available for the review
    Given a local repository with a remote default branch
    And the working tree has staged, unstaged, deleted, and untracked files
    When I run the quick review with working-tree changes
    Then the context bundle contains the fixed patch

  @QR-006
  Scenario: An unresolved quick-review ref stops the review
    Given a local repository with a remote default branch
    When I run the quick review with base "origin/missing"
    Then the quick review is incomplete with a ref error

  @QR-006
  Scenario Outline: Unsafe quick-review states stop without a success result
    Given a local repository with a remote default branch
    And the repository is prepared for quick-review failure "<failure>"
    When I run the failing quick review
    Then the quick review stops without a successful result
    And no failure context is written

    Examples:
      | failure       |
      | fetch         |
      | ambiguous ref |
      | conflict      |
      | criss-cross   |
      | unrelated     |
      | shallow       |
      | snapshot race |

  @QR-007
  Scenario: Explicit refs and direct comparison remain available
    Given a local repository with a remote default branch
    And the current branch has an unpushed commit
    When I run an explicit direct comparison
    Then the result is a direct committed review

  @QR-008
  Scenario: Both one-line quick-review forms produce review context
    Given a local repository with a remote default branch
    And the current branch has an unpushed commit
    When I run both quick-review forms
    Then both quick-review results are successful
