import asyncio
import concurrent.futures
import functools
from itertools import groupby
import logging
import multiprocessing

from coalib.core.DependencyTracker import DependencyTracker
from coalib.core.Graphs import traverse_graph


# FIXME Improve performance by using a `CoalaRunContext` class. This avoids
# FIXME   passing always parameters around like `result_callback`,
# FIXME   `dependency_tracker`, `event_loop`, `running_tasks` and `executor`.

# TODO Rename .DEPENDENCIES to .BEAR_DEPS, I'll file an issue later for
# TODO   renaming^^

def get_cpu_count():
    try:
        return multiprocessing.cpu_count()
    except NotImplementedError:  # pragma: no cover
        # cpu_count is not implemented for some CPU architectures/OSes
        return 1


def schedule_bears(bears,
                   result_callback,
                   dependency_tracker,
                   event_loop,
                   running_tasks,
                   executor):
    """
    Schedules the tasks of bears to the given executor and runs them on the
    given event loop.

    :param bears:
        A list of bear instances to be scheduled onto the process pool.
    :param result_callback:
        A callback function which is called when results are available.
    :param dependency_tracker:
        The object that keeps track of dependencies.
    :param event_loop:
        The asyncio event loop to schedule bear tasks on.
    :param running_tasks:
        Tasks that are already scheduled, organized in a dict with
        bear instances as keys and asyncio-coroutines as values containing
        their scheduled tasks.
    :param executor:
        The executor to which the bear tasks are scheduled.
    """
    for bear in bears:
        if dependency_tracker.get_dependencies(bear):  # pragma: no cover
            logging.warning(
                'Dependencies for {!r} not yet resolved, holding back. This '
                'should not happen, the dependency tracking system should be '
                'smarter.'.format(bear))
        else:
            tasks = {
                event_loop.run_in_executor(
                    executor, bear.execute_task, bear_args, bear_kwargs)
                for bear_args, bear_kwargs in bear.generate_tasks()}

            running_tasks[bear] = tasks

            for task in tasks:
                task.add_done_callback(functools.partial(
                    finish_task, bear, result_callback, dependency_tracker,
                    running_tasks, event_loop, executor))

            logging.debug('Scheduled {!r} (tasks: {})'.format(bear,
                                                              len(tasks)))

            if not tasks:
                # We need to recheck our runtime if something is left to
                # process, as when no tasks were offloaded the event-loop could
                # hang up otherwise.
                cleanup_bear(bear, result_callback, dependency_tracker,
                             running_tasks, event_loop, executor)


def cleanup_bear(bear,
                 result_callback,
                 dependency_tracker,
                 running_tasks,
                 event_loop,
                 executor):
    """
    Cleans up state of an ongoing run for a bear.

    - If the given bear has no running tasks left:
      - Resolves its dependencies.
      - Schedules dependant bears.
      - Removes the bear from the ``running_tasks`` dict.
    - Checks whether there are any remaining tasks, and quits the event loop
      accordingly if none are left.

    :param bear:
        The bear to clean up state for.
    :param result_callback:
        The result-callback handling results from bears.
    :param dependency_tracker:
        The dependency-tracker holding all relations of bears.
    :param running_tasks:
        The dict of running-tasks.
    :param event_loop:
        The event-loop tasks are scheduled on.
    :param executor:
        The executor tasks are executed on.
    """
    if not running_tasks[bear]:
        resolved_bears = dependency_tracker.resolve(bear)

        if resolved_bears:
            schedule_bears(resolved_bears, result_callback,
                           dependency_tracker, event_loop, running_tasks,
                           executor)

        del running_tasks[bear]

    if not running_tasks:
        # Check the DependencyTracker additionally for remaining
        # dependencies.
        resolved = dependency_tracker.all_dependencies_resolved
        if not resolved:  # pragma: no cover
            logging.warning(
                'Core finished with run, but it seems some dependencies '
                'were unresolved: {}. Ignoring them.'.format(', '.join(
                    repr(dependant) + ' dependent on ' + repr(dependency)
                    for dependency, dependant in dependency_tracker)))

        event_loop.stop()


def finish_task(bear,
                result_callback,
                dependency_tracker,
                running_tasks,
                event_loop,
                executor,
                task):
    """
    The callback for when a task of a bear completes. It is responsible for
    checking if the bear completed its execution and the handling of the
    result generated by the task. It also schedules new tasks if dependencies
    get resolved.

    :param bear:
        The bear that the task belongs to.
    :param result_callback:
        A callback function which is called when results are available.
    :param dependency_tracker:
        The object that keeps track of dependencies.
    :param running_tasks:
        Dictionary that keeps track of the remaining tasks of each bear.
    :param event_loop:
        The ``asyncio`` event loop bear-tasks are scheduled on.
    :param executor:
        The executor to which the bear tasks are scheduled.
    :param task:
        The task that completed.
    """
    try:
        results = task.result()

        for dependant in dependency_tracker.get_dependants(bear):
            dependant.add_dependency_results(results)
    except Exception as ex:
        # FIXME Try to display only the relevant traceback of the bear if error
        # FIXME occurred there, not the complete event-loop traceback.
        logging.error('An exception was thrown during bear execution.',
                      exc_info=ex)

        results = None

        # Unschedule/resolve dependent bears, as these can't run any more.
        dependants = dependency_tracker.get_all_dependants(bear)
        for dependant in dependants:
            dependency_tracker.resolve(dependant)
        logging.debug('Following dependent bears were unscheduled: ' +
                      ', '.join(repr(dependant) for dependant in dependants))
    finally:
        running_tasks[bear].remove(task)
        cleanup_bear(bear, result_callback, dependency_tracker, running_tasks,
                     event_loop, executor)

    if results is not None:
        for result in results:
            try:
                # FIXME Long operations on the result-callback do block the
                # FIXME   scheduler significantly. It should be possible to
                # FIXME   schedule new Python Threads on the given event_loop
                # FIXME   and process the callback there.
                result_callback(result)
            except Exception as ex:
                # FIXME Try to display only the relevant traceback of the
                # FIXME result handler if error occurred there, not the
                # FIXME complete event-loop traceback.
                logging.error(
                    'An exception was thrown during result-handling.',
                    exc_info=ex)


def initialize_dependencies(bears):
    """
    Initializes and returns a ``DependencyTracker`` instance together with a
    set of bears ready for scheduling.

    This function acquires, processes and registers bear dependencies
    accordingly using a consumer-based system, where each dependency bear has
    only a single instance per section.

    The bears set returned accounts for bears that have dependencies and
    excludes them accordingly. Dependency bears that have themselves no further
    dependencies are included so the dependency chain can be processed
    correctly.

    :param bears:
        The set of bears to run that serve as an entry-point.
    :return:
        A tuple with ``(dependency_tracker, bears_to_schedule)``.
    """
    # Pre-collect bears in a set as we use them more than once. Especially
    # remove duplicate instances.
    bears = set(bears)

    dependency_tracker = DependencyTracker()

    # For a consumer-based system, we have a situation which can be visualized
    # with a graph:
    #
    # (section1, file_dict1) (section1, file_dict2) (section2, file_dict2)
    #       |       |                  |                      |
    #       V       V                  V                      V
    #     bear1   bear2              bear3                  bear4
    #       |       |                  |                      |
    #       V       V                  |                      |
    #  BearType1  BearType2            -----------------------|
    #       |       |                                         |
    #       |       |                                         V
    #       ---------------------------------------------> BearType3
    #
    # We need to traverse this graph and instantiate dependency bears
    # accordingly, one per section.

    # Group bears by sections and file-dictionaries. These will serve as
    # entry-points for the dependency-instantiation-graph.
    grouping = groupby(bears, key=lambda bear: (bear.section, bear.file_dict))
    for (section, file_dict), bears_per_section in grouping:
        # Pre-collect bears as the iterator only works once.
        bears_per_section = list(bears_per_section)

        # Now traverse each edge of the graph, and instantiate a new dependency
        # bear if not already instantiated. For the entry point bears, we hack
        # in identity-mappings because those are already instances. Also map
        # the types of the instantiated bears to those instances, as if the
        # user already supplied an instance of a dependency, we reuse it
        # accordingly.
        type_to_instance_map = {}
        for bear in bears_per_section:
            type_to_instance_map[bear] = bear
            type_to_instance_map[type(bear)] = bear

        def instantiate_and_track(prev_bear_type, next_bear_type):
            if next_bear_type not in type_to_instance_map:
                type_to_instance_map[next_bear_type] = (
                    next_bear_type(section, file_dict))

            dependency_tracker.add(type_to_instance_map[next_bear_type],
                                   type_to_instance_map[prev_bear_type])

        traverse_graph(bears_per_section,
                       lambda bear: bear.DEPENDENCIES,
                       instantiate_and_track)

    # TODO Part this up into different function?
    # Get all bears that aren't resolved and exclude those from scheduler set.
    bears -= {bear for bear in bears
              if dependency_tracker.get_dependencies(bear)}

    # Get all bears that have no further dependencies and shall be
    # scheduled additionally.
    for dependency in dependency_tracker.dependencies:
        if not dependency_tracker.get_dependencies(dependency):
            bears.add(dependency)

    return dependency_tracker, bears


def run(bears, result_callback):
    """
    Runs a coala session.

    :param bears:
        The bear instances to run.
    :param result_callback:
        A callback function which is called when results are available. Must
        have following signature::

            def result_callback(result):
                pass
    """
    # FIXME Allow to pass different executors nicely, for example to execute
    # FIXME   coala with less cores, or to schedule jobs on distributed systems
    # FIXME   (for example Mesos).

    # Set up event loop and executor.
    event_loop = asyncio.SelectorEventLoop()
    executor = concurrent.futures.ProcessPoolExecutor(
        max_workers=get_cpu_count())

    # Initialize dependency tracking.
    dependency_tracker, bears_to_schedule = initialize_dependencies(bears)

    # Let's go.
    schedule_bears(bears_to_schedule, result_callback, dependency_tracker,
                   event_loop, {}, executor)
    try:
        event_loop.run_forever()
    finally:
        event_loop.close()
