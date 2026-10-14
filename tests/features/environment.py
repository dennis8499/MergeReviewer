def before_scenario(context, scenario):
    context.branch_fixture = None
    context.temporary_directory = None


def after_scenario(context, scenario):
    if context.branch_fixture is not None:
        context.branch_fixture.doCleanups()
    if context.temporary_directory is not None:
        context.temporary_directory.cleanup()
