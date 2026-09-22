Feature: Versioned Merge Reviewer releases

  @release @REL-001
  Scenario: A matching version tag creates an installable skill archive
    Given a release fixture with version "0.1.0"
    When I build a release package for tag "v0.1.0"
    Then the release package is created

  @release @REL-002
  Scenario: A mismatched tag stops before creating a package
    Given a release fixture with version "0.1.0"
    When I try to build a release package for tag "v0.1.1"
    Then release packaging fails with a version mismatch
    And no release package is created

  @release @REL-003
  Scenario: The archive preserves only the installable skill structure
    Given a release fixture with version "0.1.0"
    When I build a release package for tag "v0.1.0"
    Then the package root is "merge-reviewer"
    And the package contains "SKILL.md", "VERSION", "agents/openai.yaml", "references/review-rules.md", and "scripts/git_review_context.py"
    And the package excludes "tests/test_should_not_ship.py"

  @human-acceptance @REL-004
  Scenario: A matching tag publishes a GitHub release
    Given the release workflow is available on the main branch
    When I push tag "v0.1.0" to origin
    Then the GitHub Actions release workflow completes successfully
    And the release "v0.1.0" contains "merge-reviewer-0.1.0.zip"
