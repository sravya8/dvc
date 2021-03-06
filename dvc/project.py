import os
import itertools
import networkx as nx

from dvc.logger import Logger
from dvc.exceptions import DvcException
from dvc.stage import Stage, Output, Dependency
from dvc.config import Config
from dvc.state import State
from dvc.lock import Lock
from dvc.scm import SCM
from dvc.cache import Cache
from dvc.data_cloud import DataCloud


class PipelineError(DvcException):
    pass


class StageNotInPipelineError(PipelineError):
    pass


class StageNotFoundError(DvcException):
    pass


class Pipeline(object):

    def __init__(self, project, G):
        self.project = project
        self.G = G

    def graph(self):
        return self.G

    def stages(self):
        return nx.get_node_attributes(self.G, 'stage')

    def changed(self, stage):
        for node in nx.dfs_postorder_nodes(G, stage.path.relative_to(self.project.root_dir)):
            if self.stages[node].changed():
                return True
        return False

    def reproduce(self, stage):
        if stage not in self.stages():
            raise StageNotInPipelineError()

        if not self.changed(stage):
            raise PipelineNotChangedError()

        for node in nx.dfs_postorder_nodes(G, stage.path.relative_to(self.project.root_dir)):
            self.stages[node].reproduce()

        stage.reproduce()


class Project(object):
    DVC_DIR = '.dvc'

    def __init__(self, root_dir):
        self.root_dir = os.path.abspath(os.path.realpath(root_dir))
        self.dvc_dir = os.path.join(self.root_dir, self.DVC_DIR)

        self.scm = SCM(self.root_dir)
        self.lock = Lock(self.dvc_dir)
        self.cache = Cache(self.dvc_dir)
        self.state = State(self.root_dir, self.dvc_dir)
        self.config = Config(self.dvc_dir)
        self.logger = Logger()
        self.cloud = DataCloud(self.config._config)

    @staticmethod
    def init(root_dir):
        """
        Initiate dvc project in directory.

        Args:
            root_dir: Path to project's root directory.

        Returns:
            Project instance.

        Raises:
            KeyError: Raises an exception.
        """
        root_dir = os.path.abspath(root_dir)
        dvc_dir = os.path.join(root_dir, Project.DVC_DIR)
        os.mkdir(dvc_dir)

        config = Config.init(dvc_dir)
        cache = Cache.init(dvc_dir)
        state = State.init(root_dir, dvc_dir)
        lock = Lock(dvc_dir)

        scm = SCM(root_dir)
        scm.ignore_list([cache.cache_dir,
                         state.state_file,
                         lock.lock_file])

        ignore_file = os.path.join(dvc_dir, scm.ignore_file())
        scm.add([config.config_file, ignore_file])
        scm.commit('DVC init')

        return Project(root_dir)

    def add(self, fname):
        path = os.path.abspath(fname) + Stage.STAGE_FILE_SUFFIX
        cwd = os.path.dirname(path)
        outputs = [Output.loads(self, os.path.basename(fname), use_cache=True, cwd=cwd)]
        stage = Stage(project=self,
                      path=path,
                      cmd=None,
                      cwd=cwd,
                      outs=outputs,
                      deps=[],
                      locked=True)
        stage.save()
        stage.dump()
        return stage

    def remove(self, fname):
        stages = []
        output = Output.loads(self, fname)
        for out in self.outs():
            if out.path == output.path:
                stage = out.stage()
                stages.append(stage)

        if len(stages) == 0:
            raise StageNotFoundError(fname) 

        for stage in stages:
            stage.remove()

        return stages

    def _add_orphans(self, deps):
        outs = [out.path for out in self.outs()]
        for dep in deps:
            if not dep.use_cache or dep.path in outs:
                continue
            self.add(dep.path)

    def run(self,
            cmd=None,
            deps=[],
            deps_no_cache=[],
            outs=[],
            outs_no_cache=[],
            locked=False,
            fname=Stage.STAGE_FILE,
            cwd=os.curdir):
        cwd = os.path.abspath(cwd)
        path = os.path.join(cwd, fname)
        outputs = Output.loads_from(self, outs, use_cache=True, cwd=cwd)
        outputs += Output.loads_from(self, outs_no_cache, use_cache=False, cwd=cwd)
        deps = Dependency.loads_from(self, deps, use_cache=True, cwd=cwd)
        deps += Dependency.loads_from(self, deps_no_cache, use_cache=False, cwd=cwd)

        # Add files that were specified as cached dependencies and weren't added before
        self._add_orphans(deps)

        stage = Stage(project=self,
                      path=path,
                      cmd=cmd,
                      cwd=cwd,
                      outs=outputs,
                      deps=deps,
                      locked=locked)
        stage.run()
        stage.dump()
        return stage

    def reproduce(self, targets, recursive=True, force=False):
        reproduced = []
        stages = nx.get_node_attributes(self.graph(), 'stage')
        for target in targets:
            node = os.path.relpath(os.path.abspath(target), self.root_dir)
            if node not in stages:
                raise StageNotFoundError(target)

            if recursive:
                for n in nx.dfs_postorder_nodes(self.graph(), node):
                    stages[n].reproduce(force=force)
                    stages[n].dump()
                    reproduced.append(stages[n])

            stages[node].reproduce(force=force)
            stages[node].dump()
            reproduced.append(stages[node])

        return reproduced

    def checkout(self):
        for stage in self.stages():
            stage.checkout()

    def _used_cache(self):
        clist = []
        for stage in self.stages():
            for entry in itertools.chain(stage.outs, stage.deps):
                if not entry.use_cache:
                    continue
                if entry.cache not in clist:
                    clist.append(entry.cache)
        return clist

    def gc(self):
        clist = self._used_cache()
        for cache in self.cache.all():
            if cache in clist:
                continue
            os.unlink(cache)
            self.logger.info('\'{}\' was removed'.format(cache))

    def push(self, jobs=1):
        self.cloud.push(self._used_cache(), jobs)

    def pull(self, jobs=1):
        self.cloud.pull(self._used_cache(), jobs)
        for stage in self.stages():
            for entry in itertools.chain(stage.outs, stage.deps):
                if entry.use_cache:
                    entry.link()

    def status(self, jobs=1):
        return self.cloud.status(self._used_cache(), jobs)

    def graph(self):
        G = nx.DiGraph()

        for stage in self.stages():
            node = os.path.relpath(stage.path, self.root_dir)
            G.add_node(node, stage=stage)
            for dep in stage.deps:
                dep_stage = dep.stage()
                if not dep_stage:
                    continue
                dep_node = os.path.relpath(dep_stage.path, self.root_dir)
                G.add_node(dep_node, stage=dep_stage)
                G.add_edge(node, dep_node)

        return G

    def stages(self):
        stages = []
        for root, dirs, files in os.walk(self.root_dir):
            for fname in files:
                path = os.path.join(root, fname)
                if not Stage.is_stage_file(path):
                    continue
                stages.append(Stage.load(self, path))
        return stages

    def outs(self):
        outs = []
        for stage in self.stages():
            outs += stage.outs
        return outs

    def pipelines(self):
        pipelines = []
        for G in nx.weakly_connected_component_subgraphs(self.graph()):
            pipeline = Pipeline(self, G)
            pipelines.append(pipeline)

        return pipelines
