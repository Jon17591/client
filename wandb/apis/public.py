import logging
import time
import sys
import os
import json
import re
import six
import yaml
import tempfile
import datetime
from gql import Client, gql
from gql.client import RetryError
from gql.transport.requests import RequestsHTTPTransport
from six.moves import urllib
from functools import partial
import shutil
import hashlib
import base64
import requests
import platform

import wandb
from wandb.core import termlog
from wandb import Error, __version__
from wandb import artifacts
from wandb import util
from wandb.retry import retriable
from wandb.summary import HTTPSummary
from wandb import env
from wandb.apis.internal import Api as InternalApi
from wandb.apis import normalize_exceptions

logger = logging.getLogger(__name__)

WANDB_INTERNAL_KEYS = {'_wandb', 'wandb_version'}
PROJECT_FRAGMENT = '''fragment ProjectFragment on Project {
    id
    name
    entityName
    createdAt
    isBenchmark
}'''

RUN_FRAGMENT = '''fragment RunFragment on Run {
    id
    tags
    name
    displayName
    sweepName
    state
    config
    commit
    readOnly
    createdAt
    heartbeatAt
    description
    notes
    systemMetrics
    summaryMetrics
    historyLineCount
    user {
        name
        username
    }
    historyKeys
}'''

FILE_FRAGMENT = '''fragment RunFilesFragment on Run {
    files(names: $fileNames, after: $fileCursor, first: $fileLimit) {
        edges {
            node {
                id
                name
                url(upload: $upload)
                sizeBytes
                mimetype
                updatedAt
                md5
            }
            cursor
        }
        pageInfo {
            endCursor
            hasNextPage
        }
    }
}'''

ARTIFACTS_TYPES_FRAGMENT = '''
fragment ArtifactTypesFragment on ArtifactTypeConnection {
    edges {
         node {
             id
             name
             description
             createdAt
         }
         cursor
    }
    pageInfo {
        endCursor
        hasNextPage
    }
}
'''

ARTIFACT_FRAGMENT = '''
fragment ArtifactFragment on Artifact {
    id
    digest
    description
    state
    size
    createdAt
    updatedAt
    labels
    metadata
    aliases {
        artifactCollectionName
        alias
    }
}
'''

# TODO, factor out common file fragment
ARTIFACT_FILES_FRAGMENT = '''fragment ArtifactFilesFragment on Artifact {
    files(names: $fileNames, after: $fileCursor, first: $fileLimit) {
        edges {
            node {
                id
                name: displayName
                url
                sizeBytes
                mimetype
                updatedAt
                digest
                md5
            }
            cursor
        }
        pageInfo {
            endCursor
            hasNextPage
        }
    }
}'''


class RetryingClient(object):
    def __init__(self, client):
        self._client = client

    @retriable(retry_timedelta=datetime.timedelta(
        seconds=20),
        check_retry_fn=util.no_retry_auth,
        retryable_exceptions=(RetryError, requests.RequestException))
    def execute(self, *args, **kwargs):
        return self._client.execute(*args, **kwargs)


class Api(object):
    """
    Used for querying the wandb server.

    Examples:
        Most common way to initialize
        ```
            wandb.Api()
        ```

    Args:
        overrides (dict): You can set `base_url` if you are using a wandb server
            other than https://api.wandb.ai.
            You can also set defaults for `entity`, `project`, and `run`.
    """

    _HTTP_TIMEOUT = env.get_http_timeout(9)
    VIEWER_QUERY = gql('''
    query Viewer{
        viewer {
            id
            flags
            entity
            teams {
                edges {
                    node {
                        name
                    }
                }
            }
        }
    }
    ''')

    def __init__(self, overrides={}):
        self.settings = InternalApi().settings()
        if self.api_key is None:
            wandb.login()
        self.settings.update(overrides)
        if 'username' in overrides and 'entity' not in overrides:
            wandb.termwarn('Passing "username" to Api is deprecated. please use "entity" instead.')
            self.settings['entity'] = overrides['username']
        self._projects = {}
        self._runs = {}
        self._sweeps = {}
        self._reports = {}
        self._default_entity = None
        self._base_client = Client(
            transport=RequestsHTTPTransport(
                headers={'User-Agent': self.user_agent, 'Use-Admin-Privileges': "true"},
                use_json=True,
                # this timeout won't apply when the DNS lookup fails. in that case, it will be 60s
                # https://bugs.python.org/issue22889
                timeout=self._HTTP_TIMEOUT,
                auth=("api", self.api_key),
                url='%s/graphql' % self.settings['base_url']
            )
        )
        self._client = RetryingClient(self._base_client)

    def create_run(self, **kwargs):
        if kwargs.get("entity") is None:
            kwargs["entity"] = self.default_entity
        return Run.create(self, **kwargs)

    @property
    def client(self):
        return self._client

    @property
    def user_agent(self):
        return 'W&B Public Client %s' % __version__

    @property
    def api_key(self):
        auth = requests.utils.get_netrc_auth(self.settings['base_url'])
        key = None
        if auth:
            key = auth[-1]
        # Environment should take precedence
        if os.getenv("WANDB_API_KEY"):
            key = os.environ["WANDB_API_KEY"]
        return key

    @property
    def default_entity(self):
        if self._default_entity is None:
            res = self._client.execute(self.VIEWER_QUERY)
            self._default_entity = (res.get('viewer') or {}).get('entity')
        return self._default_entity

    def flush(self):
        """
        The api object keeps a local cache of runs, so if the state of the run may
            change while executing your script you must clear the local cache with `api.flush()`
            to get the latest values associated with the run."""
        self._runs = {}

    def _parse_project_path(self, path):
        """Parses paths in the following formats:

        entity/project
        project

        entity is optional and will fall back to the current logged in user
        """
        parts = path.split("/")
        entity = self.settings['entity'] or self.default_entity
        project = self.settings['project']
        if len(parts) == 1:
            project = parts[1]
        elif len(parts) == 2:
            entity, project = parts[0], parts[1]
        else:
            raise ValueError('Invalid project path: %s' % path)
        return entity, project

    def _parse_path(self, path):
        """Parses paths in the following formats:

        url: entity/project/runs/run_id
        path: entity/project/run_id
        docker: entity/project:run_id

        entity is optional and will fall back to the current logged in user.
        """
        project = self.settings['project']
        entity = self.settings['entity'] or self.default_entity
        parts = path.replace("/runs/", "/").strip("/ ").split("/")
        if ":" in parts[-1]:
            run = parts[-1].split(":")[-1]
            parts[-1] = parts[-1].split(":")[0]
        elif parts[-1]:
            run = parts[-1]
        if len(parts) > 1:
            project = parts[1]
            if entity and run == project:
                project = parts[0]
            else:
                entity = parts[0]
            if len(parts) == 3:
                entity = parts[0]
        else:
            project = parts[0]
        return entity, project, run

    def _parse_artifact_path(self, path):
        """Returns project, entity and artifact name for project specified by path"""
        project = self.settings['project']
        entity = self.settings['entity'] or self.default_entity
        if path is None:
            return entity, project
        parts = path.split('/')
        if len(parts) > 3:
            raise ValueError('Invalid artifact path: %s' % path)
        elif len(parts) == 1:
            return entity, project, path
        elif len(parts) == 2:
            return entity, parts[0], parts[1]
        return parts

    def _parse_project_path(self, path):
        """Returns project and entity for project specified by path"""
        project = self.settings['project']
        entity = self.settings['entity'] or self.default_entity
        if path is None:
            return entity, project
        parts = path.split('/', 1)
        if len(parts) == 1:
            return entity, path
        return parts

    def projects(self, entity=None, per_page=200):
        """Get projects for a given entity.
        Args:
            entity (str): Name of the entity requested.  If None will fallback to
                default entity passed to :obj:`Api`.  If no default entity, will raise a `ValueError`.
            per_page (int): Sets the page size for query pagination.  None will use the default size.
                Usually there is no reason to change this.

        Returns:
            A :obj:`Projects` object which is an iterable collection of :obj:`Project` objects.

        """
        if entity is None:
            entity = self.settings['entity'] or self.default_entity
            if entity is None:
                raise ValueError('entity must be passed as a parameter, or set in settings')
        if entity not in self._projects:
            self._projects[entity] = Projects(self.client, entity, per_page=per_page)
        return self._projects[entity]

    def reports(self, path="", name=None, per_page=50):
        """Get reports for a given project path.

        WARNING: This api is in beta and will likely change in a future release

        Args:
            path (str): path to project the report resides in, should be in the form: "entity/project"
            name (str): optional name of the report requested.
            per_page (int): Sets the page size for query pagination.  None will use the default size.
                Usually there is no reason to change this.

        Returns:
            A :obj:`Reports` object which is an iterable collection of :obj:`BetaReport` objects.
        """
        entity, project, run = self._parse_path(path)
        if entity is None:
            entity = self.settings['entity'] or self.default_entity
            if entity is None:
                raise ValueError('entity must be passed as a parameter, or set in settings')
        if name:
            name = urllib.parse.unquote(name)
        key = "/".join([entity, project, str(name)])
        if key not in self._reports:
            self._reports[key] = Reports(self.client, Project(
                self.client, entity, project, {}), name=name, per_page=per_page)
        return self._reports[key]

    def runs(self, path="", filters={}, order="-created_at", per_page=50):
        """Return a set of runs from a project that match the filters provided.
        You can filter by `config.*`, `summary.*`, `state`, `entity`, `createdAt`, etc.

        Examples:
            Find runs in my_project config.experiment_name has been set to "foo"
            ```
            api.runs(path="my_entity/my_project", {"config.experiment_name": "foo"})
            ```

            Find runs in my_project config.experiment_name has been set to "foo" or "bar"
            ```
            api.runs(path="my_entity/my_project",
                {"$or": [{"config.experiment_name": "foo"}, {"config.experiment_name": "bar"}]})
            ```

            Find runs in my_project sorted by ascending loss
            ```
            api.runs(path="my_entity/my_project", {"order": "+summary_metrics.loss"})
            ```


        Args:
            path (str): path to project, should be in the form: "entity/project"
            filters (dict): queries for specific runs using the MongoDB query language.
                You can filter by run properties such as config.key, summary_metrics.key, state, entity, createdAt, etc.
                For example: {"config.experiment_name": "foo"} would find runs with a config entry
                    of experiment name set to "foo"
                You can compose operations to make more complicated queries,
                    see Reference for the language is at  https://docs.mongodb.com/manual/reference/operator/query
            order (str): Order can be `created_at`, `heartbeat_at`, `config.*.value`, or `summary_metrics.*`.
                If you prepend order with a + order is ascending.
                If you prepend order with a - order is descending (default).
                The default order is run.created_at from newest to oldest.

        Returns:
            A :obj:`Runs` object, which is an iterable collection of :obj:`Run` objects.
        """
        entity, project = self._parse_project_path(path)
        key = path + str(filters) + str(order)
        if not self._runs.get(key):
            self._runs[key] = Runs(self.client, entity, project,
                                   filters=filters, order=order, per_page=per_page)
        return self._runs[key]

    @normalize_exceptions
    def run(self, path=""):
        """Returns a single run by parsing path in the form entity/project/run_id.

        Args:
            path (str): path to run in the form entity/project/run_id.
                If api.entity is set, this can be in the form project/run_id
                and if api.project is set this can just be the run_id.

        Returns:
            A :obj:`Run` object.
        """
        entity, project, run = self._parse_path(path)
        if not self._runs.get(path):
            self._runs[path] = Run(self.client, entity, project, run)
        return self._runs[path]

    @normalize_exceptions
    def sweep(self, path=""):
        """
        Returns a sweep by parsing path in the form entity/project/sweep_id.

        Args:
            path (str, optional): path to sweep in the form entity/project/sweep_id.  If api.entity
                is set, this can be in the form project/sweep_id and if api.project is set
                this can just be the sweep_id.

        Returns:
            A :obj:`Sweep` object.
        """
        entity, project, sweep_id = self._parse_path(path)
        if not self._sweeps.get(path):
            self._sweeps[path] = Sweep(self.client, entity, project, sweep_id)
        return self._sweeps[path]

    @normalize_exceptions
    def artifact_types(self, project=None):
        entity, project = self._parse_project_path(project)
        return ProjectArtifactTypes(self.client, entity, project)

    @normalize_exceptions
    def artifact_type(self, type_name, project=None):
        entity, project = self._parse_project_path(project)
        return ArtifactType(self.client, entity, project, type_name)

    @normalize_exceptions
    def artifact_versions(self, type_name, name, per_page=50):
        entity, project, collection_name = self._parse_artifact_path(name)
        artifact_type = ArtifactType(self.client, entity, project, type_name)
        return artifact_type.collection(collection_name).versions(per_page=per_page)

    @normalize_exceptions
    def artifact(self, name, type=None):
        """Returns a single artifact by parsing path in the form entity/project/run_id.

        Args:
            name (str): An artifact name. May be prefixed with entity/project. Valid names
                can be in the following forms:
                    name:version
                    name:alias
                    digest
            type (str, optional): The type of artifact to fetch.
        Returns:
            A :obj:`Artifact` object.
        """
        if name is None:
            raise ValueError('You must specify name= to fetch an artifact.')

        entity, project, artifact_name = self._parse_artifact_path(name)
        artifact = Artifact(self.client, entity, project, artifact_name)
        if type is not None and artifact.type != type:
            raise ValueError("type %s specified but this artifact is of type %s")
        return artifact


class Attrs(object):
    def __init__(self, attrs):
        self._attrs = attrs

    def snake_to_camel(self, string):
        camel = "".join([i.title() for i in string.split("_")])
        return camel[0].lower() + camel[1:]

    def __getattr__(self, name):
        key = self.snake_to_camel(name)
        if key == "user":
            raise AttributeError()
        if key in self._attrs.keys():
            return self._attrs[key]
        elif name in self._attrs.keys():
            return self._attrs[name]
        else:
            raise AttributeError(
                "'{}' object has no attribute '{}'".format(repr(self), name))


class Paginator(object):
    QUERY = None

    def __init__(self, client, variables, per_page=None):
        self.client = client
        self.variables = variables
        # We don't allow unbounded paging
        self.per_page = per_page
        if self.per_page is None:
            self.per_page = 50
        self.objects = []
        self.index = -1
        self.last_response = None

    def __iter__(self):
        self.index = -1
        return self

    def __len__(self):
        if self.length is None:
            self._load_page()
        if self.length is None:
            raise ValueError('Object doesn\'t provide length')
        return self.length

    @property
    def length(self):
        raise NotImplementedError()

    @property
    def more(self):
        raise NotImplementedError()

    @property
    def cursor(self):
        raise NotImplementedError()

    def convert_objects(self):
        raise NotImplementedError()

    def update_variables(self):
        self.variables.update(
            {'perPage': self.per_page, 'cursor': self.cursor})

    def _load_page(self):
        if not self.more:
            return False
        self.update_variables()
        self.last_response = self.client.execute(
            self.QUERY, variable_values=self.variables)
        self.objects.extend(self.convert_objects())
        return True

    def __getitem__(self, index):
        loaded = True
        while loaded and index > len(self.objects) - 1:
            loaded = self._load_page()
        return self.objects[index]

    def __next__(self):
        self.index += 1
        if len(self.objects) <= self.index:
            if not self._load_page():
                raise StopIteration
            if len(self.objects) <= self.index:
                raise StopIteration
        return self.objects[self.index]

    next = __next__


class User(Attrs):
    def init(self, attrs):
        super(User, self).__init__(attrs)


class Projects(Paginator):
    """
    An iterable collection of :obj:`Project` objects.
    """
    QUERY = gql('''
        query Projects($entity: String, $cursor: String, $perPage: Int = 50) {
            models(entityName: $entity, after: $cursor, first: $perPage) {
                edges {
                    node {
                        ...ProjectFragment
                    }
                    cursor
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
        %s
        ''' % PROJECT_FRAGMENT)

    def __init__(self, client, entity, per_page=50):
        self.client = client
        self.entity = entity
        variables = {
            'entity': self.entity,
        }
        super(Projects, self).__init__(client, variables, per_page)

    @property
    def length(self):
        return None

    @property
    def more(self):
        if self.last_response:
            return self.last_response['models']['pageInfo']['hasNextPage']
        else:
            return True

    @property
    def cursor(self):
        if self.last_response:
            return self.last_response['models']['edges'][-1]['cursor']
        else:
            return None

    def convert_objects(self):
        return [Project(self.client, self.entity, p["node"]["name"], p["node"])
                for p in self.last_response['models']['edges']]

    def __repr__(self):
        return "<Projects {}>".format(self.entity)


class Project(Attrs):
    """A project is a namespace for runs"""

    def __init__(self, client, entity, project, attrs):
        super(Project, self).__init__(dict(attrs))
        self.client = client
        self.name = project
        self.entity = entity

    @property
    def path(self):
        return [self.entity, self.name]

    def __repr__(self):
        return "<Project {}>".format("/".join(self.path))

    @normalize_exceptions
    def artifacts_types(self, per_page=50):
        return ProjectArtifactTypes(self.client, self.entity, self.name)


class Runs(Paginator):
    """An iterable collection of runs associated with a project and optional filter.
    This is generally used indirectly via the :obj:`Api`.runs method
    """

    QUERY = gql('''
        query Runs($project: String!, $entity: String!, $cursor: String, $perPage: Int = 50, $order: String, $filters: JSONString) {
            project(name: $project, entityName: $entity) {
                runCount(filters: $filters)
                readOnly
                runs(filters: $filters, after: $cursor, first: $perPage, order: $order) {
                    edges {
                        node {
                            ...RunFragment
                        }
                        cursor
                    }
                    pageInfo {
                        endCursor
                        hasNextPage
                    }
                }
            }
        }
        %s
        ''' % RUN_FRAGMENT)

    def __init__(self, client, entity, project, filters={}, order=None, per_page=50):
        self.entity = entity
        self.project = project
        self.filters = filters
        self.order = order
        self._sweeps = {}
        variables = {
            'project': self.project, 'entity': self.entity, 'order': self.order,
            'filters': json.dumps(self.filters)
        }
        super(Runs, self).__init__(client, variables, per_page)

    @property
    def length(self):
        if self.last_response:
            return self.last_response['project']['runCount']
        else:
            return None

    @property
    def more(self):
        if self.last_response:
            return self.last_response['project']['runs']['pageInfo']['hasNextPage']
        else:
            return True

    @property
    def cursor(self):
        if self.last_response:
            return self.last_response['project']['runs']['edges'][-1]['cursor']
        else:
            return None

    def convert_objects(self):
        objs = []
        for run_response in self.last_response['project']['runs']['edges']:
            run = Run(self.client, self.entity, self.project, run_response["node"]["name"], run_response["node"])
            objs.append(run)

            if run.sweep_name:
                if run.sweep_name in self._sweeps:
                    sweep = self._sweeps[run.sweep_name]
                else:
                    sweep = Sweep.get(self.client, self.entity, self.project,
                                      run.sweep_name, withRuns=False)
                    self._sweeps[run.sweep_name] = sweep

                run.sweep = sweep
                if run.id not in sweep.runs_by_id:
                    sweep.runs_by_id[run.id] = run
                    sweep.runs.append(run)

        return objs

    def __repr__(self):
        return "<Runs {}/{} ({})>".format(self.entity, self.project, len(self))


class Run(Attrs):
    """
    A single run associated with an entity and project.

    Attributes:
        tags ([str]): a list of tags associated with the run
        url (str): the url of this run
        id (str): unique identifier for the run (defaults to eight characters)
        name (str): the name of the run
        state (str): one of: running, finished, crashed, aborted
        config (dict): a dict of hyperparameters associated with the run
        created_at (str): ISO timestamp when the run was started
        system_metrics (dict): the latest system metrics recorded for the run
        summary (dict): A mutable dict-like property that holds the current summary.
                    Calling update will persist any changes.
        project (str): the project associated with the run
        entity (str): the name of the entity associated with the run
        user (str): the name of the user who created the run
        path (str): Unique identifier [entity]/[project]/[run_id]
        notes (str): Notes about the run
        read_only (boolean): Whether the run is editable
        history_keys (str): Keys of the history metrics that have been logged
            with `wandb.log({key: value})`
    """

    def __init__(self, client, entity, project, run_id, attrs={}):
        """
        Run is always initialized by calling api.runs() where api is an instance of wandb.Api
        """
        super(Run, self).__init__(dict(attrs))
        self.client = client
        self._entity = entity
        self.project = project
        self._files = {}
        self._base_dir = env.get_dir(tempfile.gettempdir())
        self.id = run_id
        self.sweep = None
        self.dir = os.path.join(self._base_dir, *self.path)
        try:
            os.makedirs(self.dir)
        except OSError:
            pass
        self._summary = None
        self.state = attrs.get("state", "not found")

        self.load(force=not attrs)

    @property
    def entity(self):
        return self._entity

    @property
    def username(self):
        wandb.termwarn('Run.username is deprecated. Please use Run.entity instead.')
        return self._entity

    @property
    def storage_id(self):
        # For compatibility with wandb.Run, which has storage IDs
        # in self.storage_id and names in self.id.

        return self._attrs.get('id')

    @property
    def id(self):
        return self._attrs.get('name')

    @id.setter
    def id(self, new_id):
        attrs = self._attrs
        attrs['name'] = new_id
        return new_id

    @property
    def name(self):
        return self._attrs.get('displayName')

    @name.setter
    def name(self, new_name):
        self._attrs['displayName'] = new_name
        return new_name

    @classmethod
    def create(cls, api, run_id=None, project=None, entity=None):
        """Create a run for the given project"""
        run_id = run_id or util.generate_id()
        project = project or api.settings.get("project")
        mutation = gql('''
        mutation UpsertBucket($project: String, $entity: String, $name: String!) {
            upsertBucket(input: {modelName: $project, entityName: $entity, name: $name}) {
                bucket {
                    project {
                        name
                        entity { name }
                    }
                    id
                    name
                }
                inserted
            }
        }
        ''')
        variables = {'entity': entity,
                     'project': project, 'name': run_id}
        res = api.client.execute(mutation, variable_values=variables)
        res = res['upsertBucket']['bucket']
        return Run(api.client, res["project"]["entity"]["name"], res["project"]["name"], res["name"], {
            "id": res["id"],
            "config": "{}",
            "systemMetrics": "{}",
            "summaryMetrics": "{}",
            "tags": [],
            "description": None,
            "notes": None,
            "state": "running"
        })

    def load(self, force=False):
        query = gql('''
        query Run($project: String!, $entity: String!, $name: String!) {
            project(name: $project, entityName: $entity) {
                run(name: $name) {
                    ...RunFragment
                }
            }
        }
        %s
        ''' % RUN_FRAGMENT)
        if force or not self._attrs:
            response = self._exec(query)
            if response is None or response.get('project') is None \
                    or response['project'].get('run') is None:
                raise ValueError("Could not find run %s" % self)
            self._attrs = response['project']['run']
            self.state = self._attrs['state']

            if self.sweep_name and not self.sweep:
                # There may be a lot of runs. Don't bother pulling them all
                # just for the sake of this one.
                self.sweep = Sweep.get(self.client, self.entity, self.project,
                                       self.sweep_name, withRuns=False)
                # TODO: Older runs don't always have sweeps when sweep_name is set
                if self.sweep:
                    self.sweep.runs.append(self)
                    self.sweep.runs_by_id[self.id] = self

        self._attrs['summaryMetrics'] = json.loads(
            self._attrs['summaryMetrics']) if self._attrs.get('summaryMetrics') else {}
        self._attrs['systemMetrics'] = json.loads(
            self._attrs['systemMetrics']) if self._attrs.get('systemMetrics') else {}
        if self._attrs.get('user'):
            self.user = User(self._attrs["user"])
        config_user, config_raw = {}, {}
        for key, value in six.iteritems(json.loads(self._attrs.get('config') or "{}")):
            config = config_raw if key in WANDB_INTERNAL_KEYS else config_user
            if isinstance(value, dict) and "value" in value:
                config[key] = value["value"]
            else:
                config[key] = value
        config_raw.update(config_user)
        self._attrs['config'] = config_user
        self._attrs['rawconfig'] = config_raw
        return self._attrs

    @normalize_exceptions
    def update(self):
        """
        Persists changes to the run object to the wandb backend.
        """
        mutation = gql('''
        mutation UpsertBucket($id: String!, $description: String, $display_name: String, $notes: String, $tags: [String!], $config: JSONString!) {
            upsertBucket(input: {id: $id, description: $description, displayName: $display_name, notes: $notes, tags: $tags, config: $config}) {
                bucket {
                    ...RunFragment
                }
            }
        }
        %s
        ''' % RUN_FRAGMENT)
        res = self._exec(mutation, id=self.storage_id, tags=self.tags,
                         description=self.description, notes=self.notes, display_name=self.display_name, config=self.json_config)
        self.summary.update()

    def save(self):
        self.update()

    @property
    def json_config(self):
        config = {}
        for k, v in six.iteritems(self.config):
            config[k] = {"value": v, "desc": None}
        return json.dumps(config)

    def _exec(self, query, **kwargs):
        """Execute a query against the cloud backend"""
        variables = {'entity': self.entity,
                     'project': self.project, 'name': self.id}
        variables.update(kwargs)
        return self.client.execute(query, variable_values=variables)

    def _sampled_history(self, keys, x_axis="_step", samples=500):
        spec = {"keys": [x_axis] + keys, "samples": samples}
        query = gql('''
        query Run($project: String!, $entity: String!, $name: String!, $specs: [JSONString!]!) {
            project(name: $project, entityName: $entity) {
                run(name: $name) { sampledHistory(specs: $specs) }
            }
        }
        ''')

        response = self._exec(query, specs=[json.dumps(spec)])
        return [line for line in response['project']['run']['sampledHistory']]

    def _full_history(self, samples=500, stream="default"):
        node = "history" if stream == "default" else "events"
        query = gql('''
        query Run($project: String!, $entity: String!, $name: String!, $samples: Int) {
            project(name: $project, entityName: $entity) {
                run(name: $name) { %s(samples: $samples) }
            }
        }
        ''' % node)

        response = self._exec(query, samples=samples)
        return [json.loads(line) for line in response['project']['run'][node]]

    @normalize_exceptions
    def files(self, names=[], per_page=50):
        """
        Args:
            names (list): names of the requested files, if empty returns all files
            per_page (int): number of results per page

        Returns:
            A :obj:`Files` object, which is an iterator over :obj:`File` obejcts.
        """
        return Files(self.client, self, names, per_page)

    @normalize_exceptions
    def file(self, name):
        """
        Args:
            name (str): name of requested file.

        Returns:
            A :obj:`File` matching the name argument.
        """
        return Files(self.client, self, [name])[0]

    @normalize_exceptions
    def history(self, samples=500, keys=None, x_axis="_step", pandas=True, stream="default"):
        """
        Returns sampled history metrics for a run.  This is simpler and faster if you are ok with
        the history records being sampled.

        Args:
            samples (int, optional): The number of samples to return
            pandas (bool, optional): Return a pandas dataframe
            keys (list, optional): Only return metrics for specific keys
            x_axis (str, optional): Use this metric as the xAxis defaults to _step
            stream (str, optional): "default" for metrics, "system" for machine metrics

        Returns:
            If pandas=True returns a `pandas.DataFrame` of history metrics.
            If pandas=False returns a list of dicts of history metrics.
        """
        if keys and stream != "default":
            wandb.termerror("stream must be default when specifying keys")
            return []
        elif keys:
            lines = self._sampled_history(keys=keys, x_axis=x_axis, samples=samples)
        else:
            lines = self._full_history(samples=samples, stream=stream)
        if pandas:
            pandas = util.get_module("pandas")
            if pandas:
                lines = pandas.DataFrame.from_records(lines)
            else:
                print("Unable to load pandas, call history with pandas=False")
        return lines

    @normalize_exceptions
    def scan_history(self, keys=None, page_size=1000, min_step=None, max_step=None):
        """
        Returns an iterable collection of all history records for a run.

        Example:
            Export all the loss values for an example run

            ```python
            run = api.run("l2k2/examples-numpy-boston/i0wt6xua")
            history = run.scan_history(keys=["Loss"])
            losses = [row["Loss"] for row in history]
            ```


        Args:
            keys ([str], optional): only fetch these keys, and only fetch rows that have all of keys defined.
            page_size (int, optional): size of pages to fetch from the api

        Returns:
            An iterable collection over history records (dict).
        """
        lastStep = self.lastHistoryStep
        # set defaults for min/max step
        if min_step is None:
            min_step = 0
        if max_step is None:
            max_step = lastStep + 1
        # if the max step is past the actual last step, clamp it down
        if max_step > lastStep:
            max_step = lastStep + 1
        if keys is None:
            return HistoryScan(run=self, client=self.client, page_size=page_size, min_step=min_step, max_step=max_step)
        else:
            return SampledHistoryScan(run=self, client=self.client, keys=keys, page_size=page_size, min_step=min_step, max_step=max_step)

    @normalize_exceptions
    def logged_artifacts(self, per_page=100):
        return RunArtifacts(self.client, self, mode="logged", per_page=per_page)

    @normalize_exceptions
    def used_artifacts(self, per_page=100):
        return RunArtifacts(self.client, self, mode="used", per_page=per_page)

    @property
    def summary(self):
        if self._summary is None:
            # TODO: fix the outdir issue
            self._summary = HTTPSummary(
                self, self.client, summary=self.summary_metrics)
        return self._summary

    @property
    def path(self):
        return [urllib.parse.quote_plus(str(self.entity)), urllib.parse.quote_plus(str(self.project)), urllib.parse.quote_plus(str(self.id))]

    @property
    def url(self):
        path = self.path
        path.insert(2, "runs")
        return "https://app.wandb.ai/" + "/".join(path)

    @property
    def lastHistoryStep(self):
        query = gql('''
        query Run($project: String!, $entity: String!, $name: String!) {
            project(name: $project, entityName: $entity) {
                run(name: $name) { historyKeys }
            }
        }
        ''')
        response = self._exec(query)
        if response is None or response.get('project') is None \
                or response['project'].get('run') is None or \
                response['project']['run'].get('historyKeys') is None:
            return -1
        history_keys = response['project']['run']['historyKeys']
        return history_keys['lastStep'] if 'lastStep' in history_keys else -1

    def __repr__(self):
        return "<Run {} ({})>".format("/".join(self.path), self.state)


class Sweep(Attrs):
    """A set of runs associated with a sweep
    Instantiate with:
      api.sweep(sweep_path)

    Attributes:
        runs (:obj:`Runs`): list of runs
        id (str): sweep id
        project (str): name of project
        config (str): dictionary of sweep configuration
    """

    QUERY = gql('''
    query Sweep($project: String!, $entity: String, $name: String!, $withRuns: Boolean!, $order: String) {
        project(name: $project, entityName: $entity) {
            sweep(sweepName: $name) {
                id
                name
                bestLoss
                config
                runs(order: $order) @include(if: $withRuns) {
                    edges {
                        node {
                            ...RunFragment
                        }
                        cursor
                    }
                    pageInfo {
                        endCursor
                        hasNextPage
                    }
                }
            }
        }
    }
    %s
    ''' % RUN_FRAGMENT)

    def __init__(self, client, entity, project, sweep_id, attrs={}):
        # TODO: Add agents / flesh this out.
        super(Sweep, self).__init__(dict(attrs))
        self.client = client
        self._entity = entity
        self.project = project
        self.id = sweep_id
        self.runs = []
        self.runs_by_id = {}

        self.load(force=not attrs)

    @property
    def entity(self):
        return self._entity

    @property
    def username(self):
        wandb.termwarn('Sweep.username is deprecated. please use Sweep.entity instead.')
        return self._entity

    @property
    def config(self):
        return yaml.load(self._attrs["config"])

    def load(self, force=False):
        if force or not self._attrs:
            sweep = self.get(self.client, self.entity, self.project, self.id)
            if sweep is None:
                raise ValueError("Could not find sweep %s" % self)
            self._attrs = sweep._attrs
            self.runs = sweep.runs
            self.runs_by_id = sweep.runs_by_id

        return self._attrs

    @property
    def order(self):
        if self._attrs.get("config") and self.config.get("metric"):
            sort_order = self.config["metric"].get("goal", "minimize")
            prefix = "+" if sort_order == "minimize" else "-"
            return QueryGenerator.format_order_key(prefix + self.config["metric"]["name"])

    def best_run(self, order=None):
        "Returns the best run sorted by the metric defined in config or the order passed in"
        if order is None:
            order = self.order
        else:
            order = QueryGenerator.format_order_key(order)
        if order is None:
            wandb.termwarn("No order specified and couldn't find metric in sweep config, returning most recent run")
        else:
            wandb.termlog("Sorting runs by %s" % order)
        filters = {"$and": [{"sweep": self.id}]}
        try:
            return Runs(self.client, self.entity, self.project, order=order, filters=filters, per_page=1)[0]
        except IndexError:
            return None

    @property
    def path(self):
        return [urllib.parse.quote_plus(str(self.entity)), urllib.parse.quote_plus(str(self.project)), urllib.parse.quote_plus(str(self.id))]

    @classmethod
    def get(cls, client, entity=None, project=None, sid=None, withRuns=True, order=None, query=None, **kwargs):
        """Execute a query against the cloud backend"""
        if query is None:
            query = cls.QUERY

        variables = {'entity': entity, 'project': project, 'name': sid, 'order': order, 'withRuns': withRuns}
        variables.update(kwargs)

        response = client.execute(query, variable_values=variables)
        if response.get('project') is None:
            return None
        elif response['project'].get('sweep') is None:
            return None

        sweep_response = response['project']['sweep']

        # TODO: make this paginate
        runs_response = sweep_response.get('runs')
        runs = []
        if runs_response:
            for r in runs_response['edges']:
                run = Run(client, entity, project, r["node"]["name"], r["node"])
                runs.append(run)

            del sweep_response['runs']

        sweep = cls(client, entity, project, sid, attrs=sweep_response)
        sweep.runs = runs

        for run in runs:
            sweep.runs_by_id[run.id] = run
            run.sweep = sweep

        return sweep

    def __repr__(self):
        return "<Sweep {}>".format("/".join(self.path))


class Files(Paginator):
    """Files is an iterable collection of :obj:`File` objects."""

    QUERY = gql('''
        query Run($project: String!, $entity: String!, $name: String!, $fileCursor: String,
            $fileLimit: Int = 50, $fileNames: [String] = [], $upload: Boolean = false) {
            project(name: $project, entityName: $entity) {
                run(name: $name) {
                    fileCount
                    ...RunFilesFragment
                }
            }
        }
        %s
        ''' % FILE_FRAGMENT)

    def __init__(self, client, run, names=[], per_page=50, upload=False):
        self.run = run
        variables = {
            'project': run.project, 'entity': run.entity, 'name': run.id,
            'fileNames': names, 'upload': upload
        }
        super(Files, self).__init__(client, variables, per_page)

    @property
    def length(self):
        if self.last_response:
            return self.last_response['project']['run']['fileCount']
        else:
            return None

    @property
    def more(self):
        if self.last_response:
            return self.last_response['project']['run']['files']['pageInfo']['hasNextPage']
        else:
            return True

    @property
    def cursor(self):
        if self.last_response:
            return self.last_response['project']['run']['files']['edges'][-1]['cursor']
        else:
            return None

    def update_variables(self):
        self.variables.update({'fileLimit': self.per_page, 'fileCursor': self.cursor})

    def convert_objects(self):
        return [File(self.client, r["node"])
                for r in self.last_response['project']['run']['files']['edges']]

    def __repr__(self):
        return "<Files {} ({})>".format("/".join(self.run.path), len(self))


class File(object):
    """File is a class associated with a file saved by wandb.

    Attributes:
        name (string): filename
        url (string): path to file
        md5 (string): md5 of file
        mimetype (string): mimetype of file
        updated_at (string): timestamp of last update
        size (int): size of file in bytes

    """

    def __init__(self, client, attrs):
        self.client = client
        self._attrs = attrs
        #if self.size == 0:
        #    raise AttributeError(
        #        "File {} does not exist.".format(self._attrs["name"]))

    @property
    def name(self):
        return self._attrs["name"]

    @property
    def url(self):
        return self._attrs["url"]

    @property
    def md5(self):
        return self._attrs["md5"]

    @property
    def digest(self):
        return self._attrs["digest"]

    @property
    def mimetype(self):
        return self._attrs["mimetype"]

    @property
    def updated_at(self):
        return self._attrs["updatedAt"]

    @property
    def size(self):
        sizeBytes = self._attrs['sizeBytes']
        if sizeBytes is not None:
            return int(sizeBytes)
        return 0

    @normalize_exceptions
    @retriable(retry_timedelta=datetime.timedelta(
        seconds=10),
        check_retry_fn=util.no_retry_auth,
        retryable_exceptions=(RetryError, requests.RequestException))
    def download(self, root=".", replace=False):
        """Downloads a file previously saved by a run from the wandb server.

        Args:
            replace (boolean): If `True`, download will overwrite a local file
                if it exists. Defaults to `False`.
            root (str): Local directory to save the file.  Defaults to ".".

        Raises:
            `ValueError` if file already exists and replace=False
        """
        path = os.path.join(root, self.name)
        if os.path.exists(path) and not replace:
            raise ValueError(
                "File already exists, pass replace=True to overwrite")
        util.download_file_from_url(path, self.url, Api().api_key)
        return open(path, "r")

    def __repr__(self):
        return "<File {} ({}) {}>".format(self.name, self.mimetype, util.sizeof_fmt(self.size))


class Reports(Paginator):
    """Reports is an iterable collection of :obj:`BetaReport` objects."""

    QUERY = gql('''
        query Run($project: String!, $entity: String!, $reportCursor: String,
            $reportLimit: Int = 50, $viewType: String = "runs", $viewName: String) {
            project(name: $project, entityName: $entity) {
                allViews(viewType: $viewType, viewName: $viewName, first:
                    $reportLimit, after: $reportCursor) {
                    edges {
                        node {
                            name
                            description
                            user {
                                username
                                photoUrl
                            }
                            spec
                            updatedAt
                        }
                        cursor
                    }
                }
            }
        }
        ''')

    def __init__(self, client, project, name=None, entity=None, per_page=50):
        self.project = project
        self.name = name
        variables = {
            'project': project.name, 'entity': project.entity, 'viewName': self.name
        }
        super(Reports, self).__init__(client, variables, per_page)

    @property
    def length(self):
        #TODO: Add the count the backend
        return self.per_page

    @property
    def more(self):
        if self.last_response:
            return len(self.last_response['project']['allViews']['edges']) == self.per_page
        else:
            return True

    @property
    def cursor(self):
        if self.last_response:
            return self.last_response['project']['allViews']['edges'][-1]['cursor']
        else:
            return None

    def update_variables(self):
        self.variables.update({'reportCursor': self.cursor, 'reportLimit': self.per_page})

    def convert_objects(self):
        return [BetaReport(self.client, r["node"], entity=self.project.entity, project=self.project.name)
                for r in self.last_response['project']['allViews']['edges']]

    def __repr__(self):
        return "<Reports {}>".format("/".join(self.project.path))


class QueryGenerator(object):
    """QueryGenerator is a helper object to write filters for runs"""
    INDIVIDUAL_OP_TO_MONGO = {
        '!=': '$ne',
        '>': '$gt',
        '>=': '$gte',
        '<': '$lt',
        '<=': '$lte',
        "IN": '$in',
        "NIN": '$nin',
        "REGEX": '$regex'
    }

    GROUP_OP_TO_MONGO = {
        "AND": '$and',
        "OR": '$or'
    }

    def __init__(self):
        pass

    @classmethod
    def format_order_key(self, key):
        if key.startswith("+") or key.startswith("-"):
            direction = key[0]
            key = key[1:]
        else:
            direction = "-"
        parts = key.split(".")
        if len(parts) == 1:
            # Assume the user meant summary_metrics if not a run column
            if parts[0] not in ["createdAt", "updatedAt", "name", "sweep"]:
                return direction + "summary_metrics."+parts[0]
        # Assume summary metrics if prefix isn't known
        elif parts[0] not in ["config", "summary_metrics", "tags"]:
            return direction + ".".join(["summary_metrics"] + parts)
        else:
            return direction + ".".join(parts)

    def _is_group(self, op):
        return op.get("filters") != None

    def _is_individual(self, op):
        return op.get("key") != None

    def _to_mongo_op_value(self, op, value):
        if op == "=":
            return value
        else:
            return {self.INDIVIDUAL_OP_TO_MONGO[op]: value}

    def key_to_server_path(self, key):
        if key["section"] == 'config':
            return 'config.' + key["name"]
        elif key["section"] == 'summary':
            return 'summary_metrics.' + key["name"]
        elif key["section"] == 'keys_info':
            return 'keys_info.keys.' + key["name"]
        elif key["section"] == 'run':
            return key["name"]
        elif key["section"] == 'tags':
            return 'tags.' + key["name"]
        raise ValueError("Invalid key: %s" % key)

    def _to_mongo_individual(self, filter):
        if filter["key"]["name"] == '':
            return None

        if filter.get("value") == None and filter["op"] != '=' and filter["op"] != '!=':
            return None

        if filter.get("disabled") != None and filter["disabled"]:
            return None

        if filter["key"]["section"] == 'tags':
            if filter["op"] == 'IN':
                return {"tags": {"$in": filter["value"]}}
            if filter["value"] == False:
                return {
                    "$or": [{"tags": None}, {"tags": {"$ne": filter["key"]["name"]}}]
                }
            else:
                return {"tags": filter["key"]["name"]}
        path = self.key_to_server_path(filter.key)
        if path == None:
            return path
        return {
            path: self._to_mongo_op_value(filter["op"], filter["value"])
        }

    def filter_to_mongo(self, filter):
        if self._is_individual(filter):
            return self._to_mongo_individual(filter)
        elif self._is_group(filter):
            return {
                self.GROUP_OP_TO_MONGO[filter["op"]]: [self.filter_to_mongo(f) for f in filter["filters"]]
            }


class BetaReport(Attrs):
    """BetaReport is a class associated with reports created in wandb.

    WARNING: this API will likely change in a future release

    Attributes:
        name (string): report name
        description (string): report descirpiton;
        user (:obj:User): the user that created the report
        spec (dict): the spec off the report;
        updated_at (string): timestamp of last update
    """

    def __init__(self, client, attrs, entity=None, project=None):
        self.client = client
        self.project = project
        self.entity = entity
        self.query_generator = QueryGenerator()
        super(BetaReport, self).__init__(dict(attrs))
        self._attrs["spec"] = json.loads(self._attrs["spec"])

    @property
    def sections(self):
        return self.spec['panelGroups']

    def runs(self, section, per_page=50, only_selected=True):
        run_set_idx = section.get('openRunSet', 0)
        run_set = section['runSets'][run_set_idx]
        order = self.query_generator.key_to_server_path(run_set["sort"]["key"])
        if run_set["sort"].get("ascending"):
            order = "+"+order
        else:
            order = "-"+order
        filters = self.query_generator.filter_to_mongo(run_set["filters"])
        if only_selected:
            #TODO: handle this not always existing
            filters["$or"][0]["$and"].append({"name": {"$in": run_set["selections"]["tree"]}})
        return Runs(self.client, self.entity, self.project,
                    filters=filters, order=order, per_page=per_page)

    @property
    def updated_at(self):
        return self._attrs["updatedAt"]


class HistoryScan(object):
    QUERY = gql('''
        query HistoryPage($entity: String!, $project: String!, $run: String!, $minStep: Int64!, $maxStep: Int64!, $pageSize: Int!) {
            project(name: $project, entityName: $entity) {
                run(name: $run) {
                    history(minStep: $minStep, maxStep: $maxStep, samples: $pageSize)
                }
            }
        }
        ''')

    def __init__(self, client, run, min_step, max_step, page_size=1000):
        self.client = client
        self.run = run
        self.page_size = page_size
        self.min_step = min_step
        self.max_step = max_step
        self.page_offset = min_step  # minStep for next page
        self.scan_offset = 0  # index within current page of rows
        self.rows = []  # current page of rows

    def __iter__(self):
        self.page_offset = self.min_step
        self.scan_offset = 0
        self.rows = []
        return self

    def __next__(self):
        while True:
            if self.scan_offset < len(self.rows):
                row = self.rows[self.scan_offset]
                self.scan_offset += 1
                return row
            if self.page_offset >= self.max_step:
                raise StopIteration()
            self._load_next()

    next = __next__

    @normalize_exceptions
    @retriable(
        check_retry_fn=util.no_retry_auth,
        retryable_exceptions=(RetryError, requests.RequestException))
    def _load_next(self):
        max_step = self.page_offset + self.page_size
        if max_step > self.max_step:
            max_step = self.max_step
        variables = {
            "entity": self.run.entity,
            "project": self.run.project,
            "run": self.run.id,
            "minStep": int(self.page_offset),
            "maxStep": int(max_step),
            "pageSize": int(self.page_size)
        }

        res = self.client.execute(self.QUERY, variable_values=variables)
        res = res['project']['run']['history']
        self.rows = [json.loads(row) for row in res]
        self.page_offset += self.page_size
        self.scan_offset = 0


class SampledHistoryScan(object):
    QUERY = gql('''
        query SampledHistoryPage($entity: String!, $project: String!, $run: String!, $spec: JSONString!) {
            project(name: $project, entityName: $entity) {
                run(name: $run) {
                    sampledHistory(specs: [$spec])
                }
            }
        }
        ''')

    def __init__(self, client, run, keys, min_step, max_step, page_size=1000):
        self.client = client
        self.run = run
        self.keys = keys
        self.page_size = page_size
        self.min_step = min_step
        self.max_step = max_step
        self.page_offset = min_step  # minStep for next page
        self.scan_offset = 0  # index within current page of rows
        self.rows = []  # current page of rows

    def __iter__(self):
        self.page_offset = self.min_step
        self.scan_offset = 0
        self.rows = []
        return self

    def __next__(self):
        while True:
            if self.scan_offset < len(self.rows):
                row = self.rows[self.scan_offset]
                self.scan_offset += 1
                return row
            if self.page_offset >= self.max_step:
                raise StopIteration()
            self._load_next()

    next = __next__

    @normalize_exceptions
    @retriable(
        check_retry_fn=util.no_retry_auth,
        retryable_exceptions=(RetryError, requests.RequestException))
    def _load_next(self):
        max_step = self.page_offset + self.page_size
        if max_step > self.max_step:
            max_step = self.max_step
        variables = {
            "entity": self.run.entity,
            "project": self.run.project,
            "run": self.run.id,
            "spec": json.dumps({
                "keys": self.keys,
                "minStep": int(self.page_offset),
                "maxStep": int(max_step),
                "samples": int(self.page_size)
            })
        }

        res = self.client.execute(self.QUERY, variable_values=variables)
        res = res['project']['run']['sampledHistory']
        self.rows = res[0]
        self.page_offset += self.page_size
        self.scan_offset = 0


class ProjectArtifactTypes(Paginator):
    QUERY = gql('''
        query ProjectArtifacts(
            $entityName: String!,
            $projectName: String!,
            $cursor: String,
        ) {
            project(name: $projectName, entityName: $entityName) {
                artifactTypes(after: $cursor) {
                    ...ArtifactTypesFragment
                }
            }
        }
        %s
    ''' % ARTIFACTS_TYPES_FRAGMENT)

    def __init__(self, client, entity, project, name=None, per_page=50):
        self.entity = entity
        self.project = project

        variable_values = {
            'entityName': entity,
            'projectName': project,
        }

        super(ProjectArtifactTypes, self).__init__(client, variable_values, per_page)

    @property
    def length(self):
        # TODO
        return None

    @property
    def more(self):
        if self.last_response:
            return self.last_response['project']['artifactTypes']['pageInfo']['hasNextPage']
        else:
            return True

    @property
    def cursor(self):
        if self.last_response:
            return self.last_response['project']['artifactTypes']['edges'][-1]['cursor']
        else:
            return None

    def update_variables(self):
        self.variables.update({'cursor': self.cursor})

    def convert_objects(self):
        return [ArtifactType(self.client, self.entity, self.project, r["node"]["name"], r["node"])
                for r in self.last_response['project']['artifactTypes']['edges']]


class ProjectArtifactCollections(Paginator):
    QUERY = gql('''
        query ProjectArtifactCollections(
            $entityName: String!,
            $projectName: String!,
            $artifactTypeName: String!
            $cursor: String,
        ) {
            project(name: $projectName, entityName: $entityName) {
                artifactType(name: $artifactTypeName) {
                    artifactSequences(after: $cursor) {
                        pageInfo {
                            endCursor
                            hasNextPage
                        }
                        totalCount
                        edges {
                            node {
                                id
                                name
                                description
                                createdAt
                            }
                        }
                    }
                }
            }
        }
    ''')

    def __init__(self, client, entity, project, type_name, per_page=50):
        self.entity = entity
        self.project = project
        self.type_name = type_name

        variable_values = {
            'entityName': entity,
            'projectName': project,
            'artifactTypeName': type_name
        }

        super(ProjectArtifactCollections, self).__init__(client, variable_values, per_page)

    @property
    def length(self):
        if self.last_response:
            return self.last_response['project']['artifactType']['artifactSequences']['totalCount']
        else:
            return None

    @property
    def more(self):
        if self.last_response:
            return self.last_response['project']['artifactType']['artifactSequences']['pageInfo']['hasNextPage']
        else:
            return True

    @property
    def cursor(self):
        if self.last_response:
            return self.last_response['project']['artifactType']['artifactSequences']['edges'][-1]['cursor']
        else:
            return None

    def update_variables(self):
        self.variables.update({'cursor': self.cursor})

    def convert_objects(self):
        return [ArtifactCollection(self.client, self.entity, self.project, r["node"]["name"], self.type_name, r["node"])
                for r in self.last_response['project']['artifactType']['artifactSequences']['edges']]


class RunArtifacts(Paginator):
    OUTPUT_QUERY = gql('''
        query RunArtifacts(
            $entity: String!, $project: String!, $runName: String!, $cursor: String,
        ) {
            project(name: $project, entityName: $entity) {
                run(name: $runName) {
                    outputArtifacts(after: $cursor) {
                        totalCount
                        edges {
                            node {
                                ...ArtifactFragment
                            }
                            cursor
                        }
                        pageInfo {
                            endCursor
                            hasNextPage
                        }
                    }
                }
            }
        }
        %s
    ''' % ARTIFACT_FRAGMENT)

    INPUT_QUERY = gql('''
        query RunArtifacts(
            $entity: String!, $project: String!, $runName: String!, $cursor: String,
        ) {
            project(name: $project, entityName: $entity) {
                run(name: $runName) {
                    inputArtifacts(after: $cursor) {
                        totalCount
                        edges {
                            node {
                                ...ArtifactFragment
                            }
                            cursor
                        }
                        pageInfo {
                            endCursor
                            hasNextPage
                        }
                    }
                }
            }
        }
        %s
    ''' % ARTIFACT_FRAGMENT)

    def __init__(self, client, run, mode="logged", per_page=50):
        self.run = run
        if mode == "logged":
            self.run_key = "outputArtifacts"
            self.QUERY = self.OUTPUT_QUERY
        elif mode == "used":
            self.run_key = "inputArtifacts"
            self.QUERY = self.INPUT_QUERY
        else:
            raise ValueError("mode must be logged or used")

        variable_values = {
            'entity': run.entity,
            'project': run.project,
            'runName': run.id,
        }

        super(RunArtifacts, self).__init__(client, variable_values, per_page)

    @property
    def length(self):
        if self.last_response:
            return self.last_response['project']['run'][self.run_key]['totalCount']
        else:
            return None

    @property
    def more(self):
        if self.last_response:
            return self.last_response['project']['run'][self.run_key]['pageInfo']['hasNextPage']
        else:
            return True

    @property
    def cursor(self):
        if self.last_response:
            return self.last_response['project']['run'][self.run_key]['edges']['cursor']
        else:
            return None

    def update_variables(self):
        self.variables.update({'cursor': self.cursor})

    def convert_objects(self):
        return [Artifact(self.client, self.run.entity, self.run.project, r["node"]["digest"], r["node"])
                for r in self.last_response['project']['run'][self.run_key]['edges']]


class ArtifactType(object):

    def __init__(self, client, entity, project, type_name, attrs=None):
        self.client = client
        self.entity = entity
        self.project = project
        self.type = type_name
        self._attrs = attrs
        if self._attrs is None:
            self.load()

    def load(self):
        query = gql('''
        query ProjectArtifactType(
            $entityName: String!,
            $projectName: String!,
            $artifactTypeName: String!
        ) {
            project(name: $projectName, entityName: $entityName) {
                artifactType(name: $artifactTypeName) {
                    id
                    name
                    description
                    createdAt
                }
            }
        }
        ''')
        response = self.client.execute(query, variable_values={
            'entityName': self.entity,
            'projectName': self.project,
            'artifactTypeName': self.type,
        })
        if response is None \
            or response.get('project') is None \
                or response['project'].get('artifactType') is None:
            raise ValueError("Could not find artifact type %s" % self.type)
        self._attrs = response['project']['artifactType']
        return self._attrs

    @property
    def id(self):
        return self._attrs["id"]

    @property
    def name(self):
        return self._attrs["name"]

    @normalize_exceptions
    def collections(self, per_page=50):
        """Artifact collections"""
        return ProjectArtifactCollections(self.client, self.entity, self.project, self.type)

    def collection(self, name):
        return ArtifactCollection(self.client, self.entity, self.project, name, self.type)

    def __repr__(self):
        return "<ArtifactType {}>".format(self.type)


class ArtifactCollection(object):
    def __init__(self, client, entity, project, name, type, attrs=None):
        self.client = client
        self.entity = entity
        self.project = project
        self.name = name
        self.type = type
        self._attrs = attrs

    @property
    def id(self):
        return self._attrs["id"]

    @normalize_exceptions
    def versions(self, per_page=50):
        """Artifact versions"""
        return ArtifactVersions(self.client, self.entity, self.project, self.name, self.type, per_page=per_page)

    def __repr__(self):
        return "<ArtifactCollection {} ({})>".format(self.name, self.type)


class Artifact(object):

    def __init__(self, client, entity, project, name, attrs=None):
        self.client = client
        self.entity = entity
        self.project = project
        self.artifact_name = name
        self._collection_name = None
        self._attrs = attrs
        self._metadata = {}
        if self._attrs is None:
            self._load()
        self._manifest = None
        self._is_downloaded = False

    @property
    def id(self):
        return self._attrs["id"]
    
    @property
    def metadata(self):
        return self._metadata

    # @property
    # def path(self):
    #     # TODO: This is a different style than the rest of the paths. The rest of the
    #     # paths don't include the object type (which makes them hard to distinguish).
    #     # We should maybe use URIs here.
    #     return '%s/%s/artifact/%s/%s' % (self.entity, self.project, self.artifact_type_name, self.artifact_name)

    @property
    def digest(self):
        return self._attrs["digest"]

    @property
    def state(self):
        return self._attrs["state"]

    @property
    def size(self):
        return self._attrs["size"]

    @property
    def created_at(self):
        return self._attrs["createdAt"]

    @property
    def updated_at(self):
        return self._attrs["updatedAt"] or self._attrs["createdAt"]

    @property
    def description(self):
        return self._attrs["description"]

    @description.setter
    def description(self, desc):
        self._attrs["description"] = desc

    @property
    def type(self):
        return self._attrs["artifactType"]["name"]

    @property
    def name(self):
        """Stable name you can use to fetch this artifact."""
        # TODO: All this logic should move to the backend.
        if ":" not in self.artifact_name:
            # this is a digest lookup
            return self.artifact_name
        artifact_collection_name = self.artifact_name.split(':')[0]
        for alias in self._attrs["aliases"]:
            if alias["artifactCollectionName"] == artifact_collection_name and re.match(r"^v\d+$", alias["alias"]):
                return '%s:%s' % (artifact_collection_name, alias["alias"])
        
        raise ValueError('Unexpected API result.')

    def new_file(self, name):
        raise ValueError('Cannot add files to an artifact once it has been saved')

    def add_file(self, path, name=None):
        raise ValueError('Cannot add files to an artifact once it has been saved')

    def add_dir(self, path, name=None):
        raise ValueError('Cannot add files to an artifact once it has been saved')

    def add_reference(self, path, name=None):
        raise ValueError('Cannot add files to an artifact once it has been saved')

    def get_path(self, name):
        manifest = self._load_manifest()
        storage_policy = manifest.storage_policy

        entry = manifest.entries.get(name)
        if entry is None:
            raise KeyError('Path not contained in artifact: %s' % name)

        class ArtifactEntry(object):
            @staticmethod
            def download():
                if entry.ref is not None:
                    return storage_policy.load_reference(self, name, manifest.entries[name], local=True)

                return storage_policy.load_file(self, name, manifest.entries[name])

            @staticmethod
            def ref():
                if entry.ref is not None:
                    return storage_policy.load_reference(self, name, manifest.entries[name], local=False)
                raise ValueError('Only reference entries support ref().')

        return ArtifactEntry()

    def download(self, root=None):
        """Download the artifact to dir specified by the <root>

        Args:
            root (str, optional): directory to download artifact to. If None
                artifact will be downloaded to './artifacts/<self.name>/'

        Returns:
            The path to the downloaded contents.
        """
        dirpath = root
        if dirpath is None:
            dirpath = os.path.join('.', 'artifacts', self.name)
            if platform.system() == "Windows":
                dirpath = dirpath.replace(":", "-")

        manifest = self._load_manifest()
        nfiles = len(manifest.entries)
        size = sum(e.size for e in manifest.entries.values())
        log = False
        if nfiles  > 5000 or size > 50 * 1024 * 1024:
            log = True
        if log:
            termlog('Downloading large artifact %s, %.2fMB. %s files... ' % (
                self.artifact_name, size / (1024 * 1024), nfiles), newline=False)
        start_time = time.time()

        # Force all the files to download into the same directory.
        # Download in parallel
        import multiprocessing.dummy  # this uses threads
        pool = multiprocessing.dummy.Pool(32)
        pool.map(partial(self._download_file, dirpath=dirpath), manifest.entries)
        pool.close()
        pool.join()

        self._is_downloaded = True

        if log:
            termlog('Done. %.1fs' % (time.time() - start_time), prefix=False)
        return dirpath

    def file(self, root=None):
        """Download a single file artifact to dir specified by the <root>

        Args:
            root (str, optional): directory to download artifact to. If None
                artifact will be downloaded to './artifacts/<self.name>/'

        Returns:
            The full path of the downloaded file
        """

        if root is None:
            root = os.path.join('.', 'artifacts', self.name)

        manifest = self._load_manifest()
        nfiles = len(manifest.entries)
        if nfiles > 1:
            raise ValueError("This artifact contains more than one file, call `.download()` to get all files or call .get_path(\"filename\").download()")

        return self._download_file(list(manifest.entries)[0], root)

    def _download_file(self, name, dirpath):
        # download file into cache
        cache_path = self.get_path(name).download()
        # copy file into target dir
        target_path = os.path.join(dirpath, name)
        # can't have colons in Windows
        if platform.system() == "Windows":
            target_path = target_path.replace(":", "-")

        need_copy = (not os.path.isfile(target_path)
            or os.stat(cache_path).st_mtime != os.stat(target_path).st_mtime)
        if need_copy:
            util.mkdir_exists_ok(os.path.dirname(target_path))
            # We use copy2, which preserves file metadata including modified
            # time (which we use above to check whether we should do the copy).
            shutil.copy2(cache_path, target_path)
        return target_path

    @normalize_exceptions
    def save(self):
        """
        Persists artifact changes to the wandb backend.
        """
        mutation = gql('''
        mutation updateArtifact($entity: String!, $type: String!, $project: String!, $digest: String!,
             $description: String, $metadata: JSONString, $aliases: [ArtifactAliasInput!]) {
            createArtifact(input: {
                entityName: $entity, projectName: $project, digest: $digest, artifactTypeName: $type,
                description: $description, metadata: $metadata, aliases: $aliases}) {
                artifact {
                    id
                }
            }
        }
        ''')
        res = self.client.execute(mutation, variable_values={
            "entity": self.entity,
            "project": self.project,
            "digest": self.digest,
            "description": self.description,
            "metadata": util.json_dumps_safer(self.metadata),
            "aliases": self._aliases()
        })
        return True

    def _aliases(self):
        if ":" in self.artifact_name:
            collection, alias = self.artifact_name.split(":")
            return [{"artifactCollectionName": collection, "alias": alias}]
        return []

    def verify(self, root=None):
        """Verify an artifact by checksumming its downloaded contents.

        Raises a ValueError if the verification fails. Does not verify downloaded
        reference files.

        Args:
            root (str, optional): directory to download artifact to. If None
                artifact will be downloaded to './artifacts/<self.name>/'
        """
        dirpath = root
        if dirpath is None:
            dirpath = os.path.join('.', 'artifacts', self.name)
        manifest = self._load_manifest()
        ref_count = 0
        for entry in manifest.entries.values():
            if entry.ref is None:
                if artifacts.md5_file_b64(os.path.join(dirpath, entry.path)) != entry.digest:
                    raise ValueError('Digest mismatch for file: %s' % entry.path)
            else:
                ref_count += 1
        if ref_count > 0:
            print('Warning: skipped verification of %s refs' % ref_count)

    # TODO: not yet public, but we probably want something like this.
    def _list(self):
        manifest = self._load_manifest()
        return manifest.entries.keys()

    def __repr__(self):
        return "<Artifact {}>".format(self.id)

    def _load(self):
        query = gql('''
        query Artifact(
            $entityName: String!,
            $projectName: String!,
            $name: String!
        ) {
            project(name: $projectName, entityName: $entityName) {
                artifact(name: $name) {
                    ...ArtifactFragment
                    artifactType {
                       id
                       name
                    }
                    currentManifest {
                        id
                        file {
                            id
                            url
                        }
                    }
                }
            }
        }
        %s
        ''' % ARTIFACT_FRAGMENT)
        response = self.client.execute(query, variable_values={
            'entityName': self.entity,
            'projectName': self.project,
            'name': self.artifact_name,
        })
        if response is None \
            or response.get('project') is None \
                or response['project'].get('artifact') is None:
            # we check for this after doing the call, since the backend supports raw digest lookups
            # which don't include ":" and are 32 characters long
            if ':' not in self.artifact_name and len(self.artifact_name) != 32:
                raise ValueError('Attempted to fetch artifact without alias (e.g. "<artifact_name>:v3" or "<artifact_name>:latest")')
            raise ValueError('Project %s/%s does not contain artifact: "%s"' % (
                self.entity, self.project, self.artifact_name))
        self._attrs = response['project']['artifact']
        return self._attrs

    # The only file should be wandb_manifest.json
    def _files(self, names=None, per_page=50):
        return ArtifactFiles(self.client, self, names, per_page)

    def _load_manifest(self):
        if self._manifest is None:
            index_file_url = self._attrs['currentManifest']['file']['url']
            with requests.get(index_file_url, auth=("api", Api().api_key)) as req:
                json_resp = json.loads(req.content)
                if "error" in json_resp:
                    raise ValueError("Failed to download manifest file: {}".format(json_resp["error"]))
                self._manifest = artifacts.ArtifactManifest.from_manifest_json(self, json_resp)
        return self._manifest

class ArtifactVersions(Paginator):
    """An iterable collection of artifact versions associated with a project and optional filter.
    This is generally used indirectly via the :obj:`Api`.artifact_versions method
    """

    QUERY = gql('''
        query Artifacts($project: String!, $entity: String!, $type: String!, $collection: String!, $cursor: String, $perPage: Int = 50, $order: String, $filters: JSONString) {
            project(name: $project, entityName: $entity) {
                artifactType(name: $type) {
                    artifactSequence(name: $collection) {
                        name
                        artifacts(filters: $filters, after: $cursor, first: $perPage, order: $order) {
                            totalCount
                            edges {
                                node {
                                    ...ArtifactFragment
                                }
                                version
                                cursor
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
        %s
        ''' % ARTIFACT_FRAGMENT)

    def __init__(self, client, entity, project, collection_name, type, filters={}, order=None, per_page=50):
        self.entity = entity
        self.collection_name = collection_name
        self.type = type
        self.project = project
        self.filters = filters
        self.order = order
        variables = {
            'project': self.project, 'entity': self.entity, 'order': self.order,
            'type': self.type, 'collection': self.collection_name, 'filters': json.dumps(self.filters)
        }
        super(ArtifactVersions, self).__init__(client, variables, per_page)

    @property
    def length(self):
        if self.last_response:
            return self.last_response['project']['artifactType']['artifactSequence']['artifacts']['totalCount']
        else:
            return None

    @property
    def more(self):
        if self.last_response:
            return self.last_response['project']['artifactType']['artifactSequence']['artifacts']['pageInfo']['hasNextPage']
        else:
            return True

    @property
    def cursor(self):
        if self.last_response:
            return self.last_response['project']['artifactType']['artifactSequence']['artifacts']['edges'][-1]['cursor']
        else:
            return None

    def convert_objects(self):
        if self.last_response['project']['artifactType']['artifactSequence'] is None:
            return []
        return [Artifact(self.client, self.entity, self.project, self.collection_name + ":" + a["version"], a["node"])
                for a in self.last_response['project']['artifactType']['artifactSequence']['artifacts']['edges']]

class ArtifactFiles(Paginator):
    QUERY = gql('''
        query ArtifactFiles(
            $entityName: String!,
            $projectName: String!,
            $artifactTypeName: String!,
            $artifactName: String!
            $fileNames: [String!],
            $fileCursor: String,
            $fileLimit: Int = 50
        ) {
            project(name: $projectName, entityName: $entityName) {
                artifactType(name: $artifactTypeName) {
                    artifact(name: $artifactName) {
                        ...ArtifactFilesFragment
                    }
                }
            }
        }
        %s
    ''' % ARTIFACT_FILES_FRAGMENT)

    def __init__(self, client, artifact, names=None, per_page=50):
        self.artifact = artifact
        variables = {
            'entityName': artifact.entity,
            'projectName': artifact.project,
            'artifactTypeName': artifact.artifact_type_name,
            'artifactName': artifact.artifact_name,
            'fileNames': names,
        }
        super(ArtifactFiles, self).__init__(client, variables, per_page)

    @property
    def length(self):
        # TODO
        return None

    @property
    def more(self):
        if self.last_response:
            return self.last_response['project']['artifactType']['artifact']['files']['pageInfo']['hasNextPage']
        else:
            return True

    @property
    def cursor(self):
        if self.last_response:
            return self.last_response['project']['artifactType']['artifact']['files']['edges'][-1]['cursor']
        else:
            return None

    def update_variables(self):
        self.variables.update({'fileLimit': self.per_page, 'fileCursor': self.cursor})

    def convert_objects(self):
        return [File(self.client, r["node"])
                for r in self.last_response['project']['artifactType']['artifact']['files']['edges']]

    def __repr__(self):
        return "<ArtifactFiles {} ({})>".format("/".join(self.artifact.path), len(self))
