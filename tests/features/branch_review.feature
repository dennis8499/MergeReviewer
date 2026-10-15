Feature: One Repo branch review with conditional impact extension

  Scenario: A single Repo container does not enable extension
    Given a branch fixture with 1 visible Repos
    When I prepare the explicit base and comparison branches
    Then the preparation succeeds with 1 selected Repo
    And cross Repo extension is disabled

  Scenario: A multi Repo workspace still has one selected Repo
    Given a branch fixture with 2 visible Repos
    When I prepare the explicit base and comparison branches
    Then the preparation succeeds with 1 selected Repo
    And cross Repo extension is enabled

  Scenario: A workspace file limits visible repositories
    Given a branch fixture with 3 visible Repos
    And a workspace file declaring only the selected Repo
    When I prepare the explicit base and comparison branches
    Then the preparation succeeds with 1 selected Repo
    And cross Repo extension is disabled

  Scenario Outline: Removed review entry points are rejected
    Given a branch fixture with 1 visible Repos
    When I prepare branches with the removed argument "<argument>"
    Then preparation is rejected
    Examples:
      | argument |
      | --quick |
      | --target staged |
      | --target uncommitted |
      | --scope group |
      | --mode direct |
      | --megin-receipt receipt.json |

  Scenario Outline: Non branch inputs are rejected
    Given a branch fixture with 1 visible Repos
    When I use comparison input "<input>"
    Then preparation is rejected
    Examples:
      | input |
      | HEAD |
      | refs/tags/v1 |

  Scenario: Publish one Markdown report with short branch commits and comparison details
    Given a branch fixture with 1 visible Repos
    When I prepare the explicit base and comparison branches
    And I publish the completed evidence fixture
    Then the report is in the selected Repo
    And the filename contains the timestamp and 8 character branch commits
    And the report identifies the captured branch comparison
    And the report is the only published file
    And the owned context has been removed
