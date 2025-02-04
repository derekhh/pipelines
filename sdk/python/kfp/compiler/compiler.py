# Copyright 2018-2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from collections import defaultdict
import inspect
import re
import tarfile
import zipfile
import yaml

from .. import dsl
from ._k8s_helper import K8sHelper
from ._op_to_template import _op_to_template
from ._default_transformers import add_pod_env

from ..dsl._metadata import TypeMeta, _extract_pipeline_metadata
from ..dsl._ops_group import OpsGroup

class Compiler(object):
  """DSL Compiler.

  It compiles DSL pipeline functions into workflow yaml. Example usage:
  ```python
  @dsl.pipeline(
    name='name',
    description='description'
  )
  def my_pipeline(a: dsl.PipelineParam, b: dsl.PipelineParam):
    pass

  Compiler().compile(my_pipeline, 'path/to/workflow.yaml')
  ```
  """

  def _pipelineparam_full_name(self, param):
    """_pipelineparam_full_name converts the names of pipeline parameters
      to unique names in the argo yaml

    Args:
      param(PipelineParam): pipeline parameter
      """
    if param.op_name:
      return param.op_name + '-' + param.name
    return param.name

  def _get_groups_for_ops(self, root_group):
    """Helper function to get belonging groups for each op.

    Each pipeline has a root group. Each group has a list of operators (leaf) and groups.
    This function traverse the tree and get all ancestor groups for all operators.

    Returns:
      A dict. Key is the operator's name. Value is a list of ancestor groups including the
              op itself. The list of a given operator is sorted in a way that the farthest
              group is the first and operator itself is the last.
    """
    def _get_op_groups_helper(current_groups, ops_to_groups):
      root_group = current_groups[-1]
      for g in root_group.groups:
        # Add recursive opsgroup in the ops_to_groups
        # such that the i/o dependency can be propagated to the ancester opsgroups
        if g.recursive_ref:
          ops_to_groups[g.name] = [x.name for x in current_groups] + [g.name]
          continue
        current_groups.append(g)
        _get_op_groups_helper(current_groups, ops_to_groups)
        del current_groups[-1]
      for op in root_group.ops:
        ops_to_groups[op.name] = [x.name for x in current_groups] + [op.name]

    ops_to_groups = {}
    current_groups = [root_group]
    _get_op_groups_helper(current_groups, ops_to_groups)
    return ops_to_groups

  #TODO: combine with the _get_groups_for_ops
  def _get_groups_for_opsgroups(self, root_group):
    """Helper function to get belonging groups for each opsgroup.

    Each pipeline has a root group. Each group has a list of operators (leaf) and groups.
    This function traverse the tree and get all ancestor groups for all opsgroups.

    Returns:
      A dict. Key is the opsgroup's name. Value is a list of ancestor groups including the
              opsgroup itself. The list of a given opsgroup is sorted in a way that the farthest
              group is the first and opsgroup itself is the last.
    """
    def _get_opsgroup_groups_helper(current_groups, opsgroups_to_groups):
      root_group = current_groups[-1]
      for g in root_group.groups:
        # Add recursive opsgroup in the ops_to_groups
        # such that the i/o dependency can be propagated to the ancester opsgroups
        if g.recursive_ref:
          continue
        opsgroups_to_groups[g.name] = [x.name for x in current_groups] + [g.name]
        current_groups.append(g)
        _get_opsgroup_groups_helper(current_groups, opsgroups_to_groups)
        del current_groups[-1]

    opsgroups_to_groups = {}
    current_groups = [root_group]
    _get_opsgroup_groups_helper(current_groups, opsgroups_to_groups)
    return opsgroups_to_groups

  def _get_groups(self, root_group):
    """Helper function to get all groups (not including ops) in a pipeline."""

    def _get_groups_helper(group):
      groups = {group.name: group}
      for g in group.groups:
        # Skip the recursive opsgroup because no templates
        # need to be generated for the recursive opsgroups.
        if not g.recursive_ref:
          groups.update(_get_groups_helper(g))
      return groups

    return _get_groups_helper(root_group)

  def _get_uncommon_ancestors(self, op_groups, opsgroup_groups, op1, op2):
    """Helper function to get unique ancestors between two ops.

    For example, op1's ancestor groups are [root, G1, G2, G3, op1], op2's ancestor groups are
    [root, G1, G4, op2], then it returns a tuple ([G2, G3, op1], [G4, op2]).
    """
    #TODO: extract a function for the following two code module
    if op1.name in op_groups:
      op1_groups = op_groups[op1.name]
    elif op1.name in opsgroup_groups:
      op1_groups = opsgroup_groups[op1.name]
    else:
      raise ValueError(op1.name + ' does not exist.')

    if op2.name in op_groups:
      op2_groups = op_groups[op2.name]
    elif op2.name in opsgroup_groups:
      op2_groups = opsgroup_groups[op2.name]
    else:
      raise ValueError(op1.name + ' does not exist.')

    both_groups = [op1_groups, op2_groups]
    common_groups_len = sum(1 for x in zip(*both_groups) if x==(x[0],)*len(x))
    group1 = op1_groups[common_groups_len:]
    group2 = op2_groups[common_groups_len:]
    return (group1, group2)

  def _get_condition_params_for_ops(self, root_group):
    """Get parameters referenced in conditions of ops."""

    conditions = defaultdict(set)

    def _get_condition_params_for_ops_helper(group, current_conditions_params):
      new_current_conditions_params = current_conditions_params
      if group.type == 'condition':
        new_current_conditions_params = list(current_conditions_params)
        if isinstance(group.condition.operand1, dsl.PipelineParam):
          new_current_conditions_params.append(group.condition.operand1)
        if isinstance(group.condition.operand2, dsl.PipelineParam):
          new_current_conditions_params.append(group.condition.operand2)
      for op in group.ops:
        for param in new_current_conditions_params:
          conditions[op.name].add(param)
      for g in group.groups:
        # If the subgroup is a recursive opsgroup, propagate the pipelineparams
        # in the condition expression, similar to the ops.
        if g.recursive_ref:
          for param in new_current_conditions_params:
            conditions[g.name].add(param)
        else:
          _get_condition_params_for_ops_helper(g, new_current_conditions_params)
    _get_condition_params_for_ops_helper(root_group, [])
    return conditions

  def _get_inputs_outputs(self, pipeline, root_group, op_groups, opsgroup_groups, condition_params):
    """Get inputs and outputs of each group and op.

    Returns:
      A tuple (inputs, outputs).
      inputs and outputs are dicts with key being the group/op names and values being list of
      tuples (param_name, producing_op_name). producing_op_name is the name of the op that
      produces the param. If the param is a pipeline param (no producer op), then
      producing_op_name is None.
    """
    inputs = defaultdict(set)
    outputs = defaultdict(set)

    for op in pipeline.ops.values():
      # op's inputs and all params used in conditions for that op are both considered.
      for param in op.inputs + list(condition_params[op.name]):
        # if the value is already provided (immediate value), then no need to expose
        # it as input for its parent groups.
        if param.value:
          continue
        full_name = self._pipelineparam_full_name(param)
        if param.op_name:
          upstream_op = pipeline.ops[param.op_name]
          upstream_groups, downstream_groups = self._get_uncommon_ancestors(
              op_groups, opsgroup_groups, upstream_op, op)
          for i, g in enumerate(downstream_groups):
            if i == 0:
              # If it is the first uncommon downstream group, then the input comes from
              # the first uncommon upstream group.
              inputs[g].add((full_name, upstream_groups[0]))
            else:
              # If not the first downstream group, then the input is passed down from
              # its ancestor groups so the upstream group is None.
              inputs[g].add((full_name, None))
          for i, g in enumerate(upstream_groups):
            if i == len(upstream_groups) - 1:
              # If last upstream group, it is an operator and output comes from container.
              outputs[g].add((full_name, None))
            else:
              # If not last upstream group, output value comes from one of its child.
              outputs[g].add((full_name, upstream_groups[i+1]))
        else:
          if not op.is_exit_handler:
            for g in op_groups[op.name]:
              inputs[g].add((full_name, None))
    # Generate the input/output for recursive opsgroups
    # It propagates the recursive opsgroups IO to their ancester opsgroups
    def _get_inputs_outputs_recursive_opsgroup(group):
      #TODO: refactor the following codes with the above
      if group.recursive_ref:
        params = [(param, False) for param in group.inputs]
        params.extend([(param, True) for param in list(condition_params[group.name])])
        for param, is_condition_param in params:
          if param.value:
            continue
          full_name = self._pipelineparam_full_name(param)
          if param.op_name:
            upstream_op = pipeline.ops[param.op_name]
            upstream_groups, downstream_groups = self._get_uncommon_ancestors(
              op_groups, opsgroup_groups, upstream_op, group)
            for i, g in enumerate(downstream_groups):
              if i == 0:
                inputs[g].add((full_name, upstream_groups[0]))
              # There is no need to pass the condition param as argument to the downstream ops.
              #TODO: this might also apply to ops. add a TODO here and think about it.
              elif i == len(downstream_groups) - 1 and is_condition_param:
                continue
              else:
                inputs[g].add((full_name, None))
            for i, g in enumerate(upstream_groups):
              if i == len(upstream_groups) - 1:
                outputs[g].add((full_name, None))
              else:
                outputs[g].add((full_name, upstream_groups[i+1]))
          elif not is_condition_param:
            for g in op_groups[group.name]:
              inputs[g].add((full_name, None))
      for subgroup in group.groups:
        _get_inputs_outputs_recursive_opsgroup(subgroup)
    _get_inputs_outputs_recursive_opsgroup(root_group)
    return inputs, outputs

  def _get_dependencies(self, pipeline, root_group, op_groups, opsgroups_groups, opsgroups, condition_params):
    """Get dependent groups and ops for all ops and groups.

    Returns:
      A dict. Key is group/op name, value is a list of dependent groups/ops.
      The dependencies are calculated in the following way: if op2 depends on op1,
      and their ancestors are [root, G1, G2, op1] and [root, G1, G3, G4, op2],
      then G3 is dependent on G2. Basically dependency only exists in the first uncommon
      ancesters in their ancesters chain. Only sibling groups/ops can have dependencies.
    """
    dependencies = defaultdict(set)
    for op in pipeline.ops.values():
      upstream_op_names = set()
      for param in op.inputs + list(condition_params[op.name]):
        if param.op_name:
          upstream_op_names.add(param.op_name)
      upstream_op_names |= set(op.dependent_names)

      for op_name in upstream_op_names:
        # the dependent op could be either a BaseOp or an opsgroup
        if op_name in pipeline.ops:
          upstream_op = pipeline.ops[op_name]
        elif op_name in opsgroups:
          upstream_op = opsgroups[op_name]
        else:
          raise ValueError('compiler cannot find the ' + op_name)

        upstream_groups, downstream_groups = self._get_uncommon_ancestors(
            op_groups, opsgroups_groups, upstream_op, op)
        dependencies[downstream_groups[0]].add(upstream_groups[0])

    # Generate dependencies based on the recursive opsgroups
    #TODO: refactor the following codes with the above
    def _get_dependency_opsgroup(group, dependencies):
      upstream_op_names = set()
      if group.recursive_ref:
        for param in group.inputs + list(condition_params[group.name]):
          if param.op_name:
            upstream_op_names.add(param.op_name)
      else:
        upstream_op_names = set([dependency.name for dependency in group.dependencies])

      for op_name in upstream_op_names:
        if op_name in pipeline.ops:
          upstream_op = pipeline.ops[op_name]
        elif op_name in opsgroups_groups:
          upstream_op = opsgroups_groups[op_name]
        else:
          raise ValueError('compiler cannot find the ' + op_name)
        upstream_groups, downstream_groups = self._get_uncommon_ancestors(
            op_groups, opsgroups_groups, upstream_op, group)
        dependencies[downstream_groups[0]].add(upstream_groups[0])

      for subgroup in group.groups:
        _get_dependency_opsgroup(subgroup, dependencies)

    _get_dependency_opsgroup(root_group, dependencies)

    return dependencies

  def _resolve_value_or_reference(self, value_or_reference, potential_references):
    """_resolve_value_or_reference resolves values and PipelineParams, which could be task parameters or input parameters.

    Args:
      value_or_reference: value or reference to be resolved. It could be basic python types or PipelineParam
      potential_references(dict{str->str}): a dictionary of parameter names to task names
      """
    if isinstance(value_or_reference, dsl.PipelineParam):
      parameter_name = self._pipelineparam_full_name(value_or_reference)
      task_names = [task_name for param_name, task_name in potential_references if param_name == parameter_name]
      if task_names:
        task_name = task_names[0]
        # When the task_name is None, the parameter comes directly from ancient ancesters
        # instead of parents. Thus, it is resolved as the input parameter in the current group.
        if task_name is None:
          return '{{inputs.parameters.%s}}' % parameter_name
        else:
          return '{{tasks.%s.outputs.parameters.%s}}' % (task_name, parameter_name)
      else:
        return '{{inputs.parameters.%s}}' % parameter_name
    else:
      return str(value_or_reference)

  def _group_to_template(self, group, inputs, outputs, dependencies):
    """Generate template given an OpsGroup.

    inputs, outputs, dependencies are all helper dicts.
    """
    template = {'name': group.name}

    # Generate inputs section.
    if inputs.get(group.name, None):
      template_inputs = [{'name': x[0]} for x in inputs[group.name]]
      template_inputs.sort(key=lambda x: x['name'])
      template['inputs'] = {
        'parameters': template_inputs
      }
    # Generate outputs section.
    if outputs.get(group.name, None):
      template_outputs = []
      for param_name, dependent_name in outputs[group.name]:
        template_outputs.append({
          'name': param_name,
          'valueFrom': {
            'parameter': '{{tasks.%s.outputs.parameters.%s}}' % (dependent_name, param_name)
          }
        })
      template_outputs.sort(key=lambda x: x['name'])
      template['outputs'] = {'parameters': template_outputs}

    # Generate tasks section.
    tasks = []
    for sub_group in group.groups + group.ops:
      is_recursive_subgroup = (isinstance(sub_group, OpsGroup) and sub_group.recursive_ref)
      # Special handling for recursive subgroup: use the existing opsgroup name
      if is_recursive_subgroup:
        task = {
            'name': sub_group.recursive_ref.name,
            'template': sub_group.recursive_ref.name,
        }
      else:
        task = {
          'name': sub_group.name,
          'template': sub_group.name,
        }
      if isinstance(sub_group, dsl.OpsGroup) and sub_group.type == 'condition':
        subgroup_inputs = inputs.get(sub_group.name, [])
        condition = sub_group.condition
        operand1_value = self._resolve_value_or_reference(condition.operand1, subgroup_inputs)
        operand2_value = self._resolve_value_or_reference(condition.operand2, subgroup_inputs)
        task['when'] = '{} {} {}'.format(operand1_value, condition.operator, operand2_value)

      # Generate dependencies section for this task.
      if dependencies.get(sub_group.name, None):
        group_dependencies = list(dependencies[sub_group.name])
        group_dependencies.sort()
        task['dependencies'] = group_dependencies

      # Generate arguments section for this task.
      if inputs.get(sub_group.name, None):
        arguments = []
        for param_name, dependent_name in inputs[sub_group.name]:
          if dependent_name:
            # The value comes from an upstream sibling.
            # Special handling for recursive subgroup: argument name comes from the existing opsgroup
            if is_recursive_subgroup:
              for index, input in enumerate(sub_group.inputs):
                if param_name == self._pipelineparam_full_name(input):
                  break
              referenced_input = sub_group.recursive_ref.inputs[index]
              full_name = self._pipelineparam_full_name(referenced_input)
              arguments.append({
                  'name': full_name,
                  'value': '{{tasks.%s.outputs.parameters.%s}}' % (dependent_name, param_name)
              })
            else:
              arguments.append({
                'name': param_name,
                'value': '{{tasks.%s.outputs.parameters.%s}}' % (dependent_name, param_name)
              })
          else:
            # The value comes from its parent.
            # Special handling for recursive subgroup: argument name comes from the existing opsgroup
            if is_recursive_subgroup:
              for index, input in enumerate(sub_group.inputs):
                if param_name == self._pipelineparam_full_name(input):
                  break
              referenced_input = sub_group.recursive_ref.inputs[index]
              full_name = self._pipelineparam_full_name(referenced_input)
              arguments.append({
                  'name': full_name,
                  'value': '{{inputs.parameters.%s}}' % param_name
              })
            else:
              arguments.append({
                'name': param_name,
                'value': '{{inputs.parameters.%s}}' % param_name
              })
        arguments.sort(key=lambda x: x['name'])
        task['arguments'] = {'parameters': arguments}
      tasks.append(task)
    tasks.sort(key=lambda x: x['name'])
    template['dag'] = {'tasks': tasks}
    return template

  def _create_templates(self, pipeline, op_transformers=None, op_to_templates_handler=None):
    """Create all groups and ops templates in the pipeline.

    Args:
      pipeline: Pipeline context object to get all the pipeline data from.
      op_transformers: A list of functions that are applied to all ContainerOp instances that are being processed.
      op_to_templates_handler: Handler which converts a base op into a list of argo templates.
    """

    op_to_templates_handler = op_to_templates_handler or (lambda op : [_op_to_template(op)])
    new_root_group = pipeline.groups[0]

    # Call the transformation functions before determining the inputs/outputs, otherwise
    # the user would not be able to use pipeline parameters in the container definition
    # (for example as pod labels) - the generated template is invalid.
    for op in pipeline.ops.values():
      for transformer in op_transformers or []:
        transformer(op)

    # Generate core data structures to prepare for argo yaml generation
    #   op_groups: op name -> list of ancestor groups including the current op
    #   opsgroups: a dictionary of ospgroup.name -> opsgroup
    #   inputs, outputs: group/op names -> list of tuples (full_param_name, producing_op_name)
    #   condition_params: recursive_group/op names -> list of pipelineparam
    #   dependencies: group/op name -> list of dependent groups/ops.
    # Special Handling for the recursive opsgroup
    #   op_groups also contains the recursive opsgroups
    #   condition_params from _get_condition_params_for_ops also contains the recursive opsgroups
    #   groups does not include the recursive opsgroups
    opsgroups = self._get_groups(new_root_group)
    op_groups = self._get_groups_for_ops(new_root_group)
    opsgroups_groups = self._get_groups_for_opsgroups(new_root_group)
    condition_params = self._get_condition_params_for_ops(new_root_group)
    inputs, outputs = self._get_inputs_outputs(pipeline, new_root_group, op_groups, opsgroups_groups, condition_params)
    dependencies = self._get_dependencies(pipeline, new_root_group, op_groups, opsgroups_groups, opsgroups, condition_params)

    templates = []
    for opsgroup in opsgroups.keys():
      template = self._group_to_template(opsgroups[opsgroup], inputs, outputs, dependencies)
      templates.append(template)

    for op in pipeline.ops.values():
      templates.extend(op_to_templates_handler(op))
    return templates

  def _create_volumes(self, pipeline):
    """Create volumes required for the templates"""
    volumes = []
    volume_name_set = set()
    for op in pipeline.ops.values():
      if op.volumes:
        for v in op.volumes:
          # Remove volume duplicates which have the same name
          #TODO: check for duplicity based on the serialized volumes instead of just name.
          if v['name'] not in volume_name_set:
            volume_name_set.add(v['name'])
            volumes.append(v)
    volumes.sort(key=lambda x: x['name'])
    return volumes

  def _create_pipeline_workflow(self, args, pipeline, op_transformers=None):
    """Create workflow for the pipeline."""

    # Input Parameters
    input_params = []
    for arg in args:
      param = {'name': arg.name}
      if arg.value is not None:
        param['value'] = str(arg.value)
      input_params.append(param)

    # Templates
    templates = self._create_templates(pipeline, op_transformers)
    templates.sort(key=lambda x: x['name'])

    # Exit Handler
    exit_handler = None
    if pipeline.groups[0].groups:
      first_group = pipeline.groups[0].groups[0]
      if first_group.type == 'exit_handler':
        exit_handler = first_group.exit_op

    # Volumes
    volumes = self._create_volumes(pipeline)

    # The whole pipeline workflow
    pipeline_name = pipeline.name or 'Pipeline'
    workflow = {
      'apiVersion': 'argoproj.io/v1alpha1',
      'kind': 'Workflow',
      'metadata': {'generateName': pipeline_name + '-'},
      'spec': {
        'entrypoint': pipeline_name,
        'templates': templates,
        'arguments': {'parameters': input_params},
        'serviceAccountName': 'pipeline-runner'
      }
    }
    if len(pipeline.conf.image_pull_secrets) > 0:
      image_pull_secrets = []
      for image_pull_secret in pipeline.conf.image_pull_secrets:
        image_pull_secrets.append(K8sHelper.convert_k8s_obj_to_json(image_pull_secret))
      workflow['spec']['imagePullSecrets'] = image_pull_secrets

    if pipeline.conf.timeout:
      workflow['spec']['activeDeadlineSeconds'] = pipeline.conf.timeout

    if exit_handler:
      workflow['spec']['onExit'] = exit_handler.name
    if volumes:
      workflow['spec']['volumes'] = volumes
    return workflow

  def _validate_exit_handler(self, pipeline):
    """Makes sure there is only one global exit handler.

    Note this is a temporary workaround until argo supports local exit handler.
    """

    def _validate_exit_handler_helper(group, exiting_op_names, handler_exists):
      if group.type == 'exit_handler':
        if handler_exists or len(exiting_op_names) > 1:
          raise ValueError('Only one global exit_handler is allowed and all ops need to be included.')
        handler_exists = True

      if group.ops:
        exiting_op_names.extend([x.name for x in group.ops])

      for g in group.groups:
        _validate_exit_handler_helper(g, exiting_op_names, handler_exists)

    return _validate_exit_handler_helper(pipeline.groups[0], [], False)

  def _compile(self, pipeline_func):
    """Compile the given pipeline function into workflow."""

    argspec = inspect.getfullargspec(pipeline_func)

    # Create the arg list with no default values and call pipeline function.
    # Assign type information to the PipelineParam
    pipeline_meta = _extract_pipeline_metadata(pipeline_func)
    pipeline_name = K8sHelper.sanitize_k8s_name(pipeline_meta.name)

    args_list = []
    for arg_name in argspec.args:
      arg_type = TypeMeta()
      for input in pipeline_meta.inputs:
        if arg_name == input.name:
          arg_type = input.param_type
          break
      args_list.append(dsl.PipelineParam(K8sHelper.sanitize_k8s_name(arg_name), param_type = arg_type))

    with dsl.Pipeline(pipeline_name) as p:
      pipeline_func(*args_list)

    # Remove when argo supports local exit handler.
    self._validate_exit_handler(p)

    # Fill in the default values.
    args_list_with_defaults = [dsl.PipelineParam(K8sHelper.sanitize_k8s_name(arg_name))
                               for arg_name in argspec.args]
    if argspec.defaults:
      for arg, default in zip(reversed(args_list_with_defaults), reversed(argspec.defaults)):
        arg.value = default.value if isinstance(default, dsl.PipelineParam) else default

    # Sanitize operator names and param names
    sanitized_ops = {}
    # pipeline level artifact location
    artifact_location = p.conf.artifact_location

    for op in p.ops.values():
      # inject pipeline level artifact location into if the op does not have
      # an artifact location config already.
      if artifact_location and not op.artifact_location:
        op.artifact_location = artifact_location

      sanitized_name = K8sHelper.sanitize_k8s_name(op.name)
      op.name = sanitized_name
      for param in op.outputs.values():
        param.name = K8sHelper.sanitize_k8s_name(param.name)
        if param.op_name:
          param.op_name = K8sHelper.sanitize_k8s_name(param.op_name)
      if op.output is not None:
        op.output.name = K8sHelper.sanitize_k8s_name(op.output.name)
        op.output.op_name = K8sHelper.sanitize_k8s_name(op.output.op_name)
      if op.dependent_names:
        op.dependent_names = [K8sHelper.sanitize_k8s_name(name) for name in op.dependent_names]
      if isinstance(op, dsl.ContainerOp) and op.file_outputs is not None:
        sanitized_file_outputs = {}
        for key in op.file_outputs.keys():
          sanitized_file_outputs[K8sHelper.sanitize_k8s_name(key)] = op.file_outputs[key]
        op.file_outputs = sanitized_file_outputs
      elif isinstance(op, dsl.ResourceOp) and op.attribute_outputs is not None:
        sanitized_attribute_outputs = {}
        for key in op.attribute_outputs.keys():
          sanitized_attribute_outputs[K8sHelper.sanitize_k8s_name(key)] = \
            op.attribute_outputs[key]
        op.attribute_outputs = sanitized_attribute_outputs
      sanitized_ops[sanitized_name] = op
    p.ops = sanitized_ops

    op_transformers = [add_pod_env]
    op_transformers.extend(p.conf.op_transformers)
    workflow = self._create_pipeline_workflow(args_list_with_defaults, p, op_transformers)
    return workflow

  def compile(self, pipeline_func, package_path, type_check=True):
    """Compile the given pipeline function into workflow yaml.

    Args:
      pipeline_func: pipeline functions with @dsl.pipeline decorator.
      package_path: the output workflow tar.gz file path. for example, "~/a.tar.gz"
      type_check: whether to enable the type check or not, default: False.
    """
    import kfp
    type_check_old_value = kfp.TYPE_CHECK
    try:
      kfp.TYPE_CHECK = type_check
      workflow = self._compile(pipeline_func)
      yaml.Dumper.ignore_aliases = lambda *args : True
      yaml_text = yaml.dump(workflow, default_flow_style=False)

      if package_path.endswith('.tar.gz') or package_path.endswith('.tgz'):
        from contextlib import closing
        from io import BytesIO
        with tarfile.open(package_path, "w:gz") as tar:
          with closing(BytesIO(yaml_text.encode())) as yaml_file:
            tarinfo = tarfile.TarInfo('pipeline.yaml')
            tarinfo.size = len(yaml_file.getvalue())
            tar.addfile(tarinfo, fileobj=yaml_file)
      elif package_path.endswith('.zip'):
        with zipfile.ZipFile(package_path, "w") as zip:
          zipinfo = zipfile.ZipInfo('pipeline.yaml')
          zipinfo.compress_type = zipfile.ZIP_DEFLATED
          zip.writestr(zipinfo, yaml_text)
      elif package_path.endswith('.yaml') or package_path.endswith('.yml'):
          with open(package_path, 'w') as yaml_file:
            yaml_file.write(yaml_text)
      else:
        raise ValueError('The output path '+ package_path + ' should ends with one of the following formats: [.tar.gz, .tgz, .zip, .yaml, .yml]')
    finally:
      kfp.TYPE_CHECK = type_check_old_value


