#  Copyright 2020 HPS/SAFARI Research Groups
#
#  Permission is hereby granted, free of charge, to any person obtaining a copy of
#  this software and associated documentation files (the "Software"), to deal in
#  the Software without restriction, including without limitation the rights to
#  use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
#  of the Software, and to permit persons to whom the Software is furnished to do
#  so, subject to the following conditions:
#
#  The above copyright notice and this permission notice shall be included in all
#  copies or substantial portions of the Software.
#
#  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#  SOFTWARE.

import argparse
import distutils.dir_util
import glob
from itertools import product
from pathlib import Path
import math
import multiprocessing
import os
import subprocess
import re
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from scarab_globals import *
from scarab_globals.scarab_batch_types import *

def define_commandline_arguments():
  parser = argparse.ArgumentParser(
      description='Creates checkpoints for list of programs given in the script file')

  phase_group = parser.add_argument_group(
      title='Phase Selection',
      description=('Knobs for selective enabling of different phases (If no '
                  'phase is specified, the scripts runs all phases)'))
  phase_group.add_argument(
      '-simp', '--run_simpoints',
      action='store_true',
      help='Run the benchmarks using Simpoints to get representative regions')
  phase_group.add_argument(
      '-ckpt', '--create_checkpoints',
      action='store_true',
      help='Create checkpoint for all the selected simpoints')
  phase_group.add_argument(
      '-desc', '--create_descriptor_file',
      action='store_true',
      help='Create a checkpoint descriptor file for the created checkpoints')

  ##################### Environment Configuration #####################
  parser.add_argument(
      '-m', '--mode',
      choices=['pbs', 'local'],
      default='local',
      help='Specify the platform to run on')
  parser.add_argument(
      '-o', '--output_dir',
      default='./checkpoints',
      help='Path to the descriptor file containting list of programs to get checkpoint for')
  parser.add_argument(
      '-dp', '--checkpoint_descriptor_path',
      default='./checkpoints/checkpoint_descriptor.def',
      help='Path to the descriptor file containting list of programs to get checkpoint for')
  parser.add_argument(
      '-f', '--force_write',
      action='store_true',
      help='Overwrite checkpoints if they exists')
  parser.add_argument(
      '-n', '--num_threads',
      type=int,
      help='Number of parallel threads to run')

  ################### Simpoints Configuration ###################
  parser.add_argument(
      '--max_num_simpoints',
      type=int,
      default=5,
      help='Maximum number of simpoints')
  parser.add_argument(
      '--simpoint_length',
      type=int,
      default=200000000,
      help='The number of instructions in each simpoint')
  parser.add_argument(
      '--min_simpoint_weight',
      type=float,
      default=0.1,
      help='Minimum simpoint weight to keep')
  return parser.parse_args()

def define_programs_list(descriptor_path):
  #scarab_batch.import_descriptor(descriptor_path)
  with open(descriptor_path, 'r') as f:
    exec(f.read(), globals(), globals())
  return scarab_batch_types.programs_list

def initialize_globals():
  global __args__
  global __benchmarks__

  __args__ = define_commandline_arguments()
  __benchmarks__ = define_programs_list(__args__.checkpoint_descriptor_path)

  for benchmark in __benchmarks__:
    benchmark.make(__args__.output_dir)

def verify_run_dirs():
  for benchmark in __benchmarks__:
    run_dir_path = os.path.abspath(benchmark._results_dir(__args__.output_dir) if benchmark.copy else benchmark.path)
    if not os.path.isdir(run_dir_path):
      print('ERROR: run directory for benchmark {} is not created '
            'correctly at {}'.format(benchmark, run_dir_path))
      sys.exit(1)

def fix_simpoint_scripts():
  for file_name in ['simpoint.py', 'regions.py', 'pcregions.py']:
    if 'PIN_ROOT' not in os.environ:
      print('ERROR: PIN_ROOT environment variable is not set.')
      sys.exit(1)

    file_path = os.environ['PIN_ROOT'] + '/extras/pinplay/scripts/' + file_name
    if not os.path.isfile(file_path):
      print('ERROR: could not find a file at {}. Make sure you have set the '
            'environment variable PIN_ROOT correctly.'.format(file_path))
      sys.exit(1)

    with open(file_path, 'r') as f:
      file_lines = f.readlines()

    if file_lines[0][0:2] == '#!':
      file_lines[0] = '#!/usr/bin/env python2\n'
      with open(file_path, 'w') as f:
        f.writelines(file_lines)
    else:
      print('Warining: expected file at {} to start with a shebang line'.format(file_path))

def get_submission_system():
  return command.SubmissionSystems[__args__.mode.upper()]

SIMPOINTS_RUN_CMD_TEMPLATE = (
'export PATH=$PIN_ROOT/extras/pinplay/scripts/:$PATH\n'
'export PATH=$PIN_ROOT/extras/pinplay/PinPoints/bin/:$PATH\n'
'export OMP_NUM_THREADS=1\n'
'cd {run_dir_path}\n'
'setarch x86_64 -R $PIN_ROOT/pin '
'-t $PIN_ROOT/extras/pinplay/bin/intel64/pinplay-driver.so -bbprofile '
'-slice_size {slice_size} -o {simpoints_relative_dir}/simpoints -- '
'{run_command}\n'
'simpoint.py --data_dir {simpoints_relative_dir} --bbv_file simpoints.T.0.bb '
'--simpoint_file simpoints -f 0 --maxk {maxk}\n')

def setup_simpoint_dir_and_get_simpoint_commands():
  run_simpoint_commands = []
  for benchmark in __benchmarks__:
    run_name = benchmark.name
    run_dir_path = os.path.abspath(benchmark._results_dir(__args__.output_dir) if benchmark.copy else benchmark.path)
    run_cmd = benchmark.run_cmd

    simpoints_dir_path = '{}/simpoints_{}.Data'.format(run_dir_path, run_name)
    os.makedirs(simpoints_dir_path, exist_ok=True)

    with open('{}/BENCHMARK_RUN_CMD'.format(simpoints_dir_path), 'w') as f:
      f.write(run_cmd)

      cmd_file_path = '{}/SIMPOINTS_RUN_CMD'.format(simpoints_dir_path)
      with open(cmd_file_path, 'w') as f:
        f.write(SIMPOINTS_RUN_CMD_TEMPLATE.format(
            run_dir_path=run_dir_path,
            slice_size=__args__.simpoint_length,
            simpoints_relative_dir='simpoints_{}.Data'.format(run_name),
            run_command=run_cmd,
            maxk=__args__.max_num_simpoints))

      # Append the command to run simpoint to global runfile for later use.
      run_simpoint_commands.append(
          command.generate(get_submission_system(),
                           'bash ' + cmd_file_path,
                           stdout=simpoints_dir_path +'/simpoints_cmd.out',
                           stderr=simpoints_dir_path +'/simpoints_cmd.err'))
  return run_simpoint_commands

def run_simpoints_phase():
  print('================================================')
  print('Verify all run directories are created properly...')
  verify_run_dirs()
  fix_simpoint_scripts()
  print('Running Simpoints on all benchmarks ...')
  cmds = setup_simpoint_dir_and_get_simpoint_commands()
  command.launch(get_submission_system(), cmds)

def get_subinput_numbers_for_csv_paths(simpoint_csv_paths):
  subinput_numbers = []
  for path in simpoint_csv_paths:
    dir_name = os.path.basename(os.path.dirname(path))
    m = re.match(r'simpoints_(\d+).Data', dir_name)
    if m is None:
      print('ERROR: unexpected simpoint dirctory name {}, in path {}'.format(
          dir_name, path))
      sys.exit(1)
    subinput_numbers.append(int(m.group(1)))

  return subinput_numbers

def parse_simpoint_csv_path(simpoint_csv_path):
  weights = []
  icounts = []
  simpoint_nums = []
  with open(simpoint_csv_path) as f:
    for line in f.readlines():
      if len(line.strip()) == 0:
        continue
      elif line.strip()[0] == '#':
        continue
      words = line.strip().split(',')
      simpoint_nums.append(int(words[2]))
      icounts.append(int(words[3]))
      weights.append(float(words[5]))

  while min(weights) < __args__.min_simpoint_weight:
    assert math.isclose(sum(weights), 1.0, abs_tol=1e-4)
    min_idx = weights.index(min(weights))
    del weights[min_idx]
    del icounts[min_idx]
    del simpoint_nums[min_idx]
    sum_weights = sum(weights)
    weights = [w / sum_weights for w in weights]

  weights_map = {}
  icounts_map = {}
  for simpoint_num, weight, icount in zip(simpoint_nums, weights, icounts):
    weights_map[simpoint_num] = weight
    icounts_map[simpoint_num] = icount

  return weights_map, icounts_map

def read_all_simpoints():
  simpoints = {}
  for benchmark in __benchmarks__:
    workload_path = os.path.abspath(benchmark._results_dir(__args__.output_dir) if benchmark.copy else benchmark.path)
    workload_name = benchmark.name
    simpoint_csv_path = '{}/simpoints_{}.Data/simpoints_{}.pinpoints.csv'.format(workload_path, workload_name, workload_name)
    if not os.path.isfile(simpoint_csv_path):
      print('ERROR: could not find the simpoint output file in {}'.format(simpoint_csv_path))
      sys.exit(1)

    weights_map, icounts_map = parse_simpoint_csv_path(simpoint_csv_path)
    for checkpoint_num in weights_map:
      simpoints[(benchmark.name, checkpoint_num)] = (benchmark,
          weights_map[checkpoint_num], icounts_map[checkpoint_num])

  return simpoints

CREATE_CHECKPOINT_CMD_TEMPLATE = (
'export OMP_NUM_THREADS=1 && '
'cd {checkpoint_creator_dir} && '
'ICOUNT={icount} RUN_DIR={run_dir_path} PIN_APP_COMMAND="{run_command}" '
'CHECKPOINT_PATH={checkpoint_path} make checkpoint')

def get_create_checkpoint_command(workload_path, benchmark_name,
                                  checkpoint_num, weight, icount):
  run_command_file_path = ('{}/simpoints_{}.Data/BENCHMARK_RUN_CMD'.format(workload_path, benchmark_name))
  with open(run_command_file_path) as f:
    run_command = f.read()
  workload_parent = Path(workload_path).parent

  checkpoint_path = ('{workload_parent}/{benchmark_name}_'
                     'checkpoint{checkpoint_num}_{weight}_{icount}'.format(
      workload_parent=workload_parent,
      benchmark_name=benchmark_name,
      checkpoint_num=checkpoint_num,
      weight=weight,
      icount=icount))
  os.makedirs(checkpoint_path, exist_ok=__args__.force_write)
  checkpoint_rundir_path = ('{}/RUN_DIR'.format(checkpoint_path))
  with open(checkpoint_path + '/CREATE_CMD', 'w') as f:
    f.write(CREATE_CHECKPOINT_CMD_TEMPLATE.format(
          checkpoint_creator_dir=scarab_paths.checkpoint_creator_dir,
          icount=icount,
          #run_dir_path=workload_path,
          run_dir_path=checkpoint_rundir_path,
          run_command=run_command,
          checkpoint_path=checkpoint_path))
  distutils.dir_util.copy_tree(workload_path, checkpoint_rundir_path) 

  return command.generate(
      get_submission_system(),
      'bash {}/CREATE_CMD'.format(checkpoint_path),
      stdout=checkpoint_path +'/creation_log.out',
      stderr=checkpoint_path +'/creation_log.err')

def setup_checkpoint_dirs_and_get_create_commands(simpoints):
  create_checkpoint_commands = []
  for key_tuple in simpoints:
    benchmark_name, checkpoint_num = key_tuple
    benchmark, weight, icount = simpoints[key_tuple]
    workload_path = os.path.abspath(benchmark._results_dir(__args__.output_dir) if benchmark.copy else benchmark.path)
    create_checkpoint_commands.append(get_create_checkpoint_command(
          workload_path, benchmark_name, checkpoint_num, weight, icount))

  return create_checkpoint_commands

def create_checkpoints_phase():
  print('================================================')
  print('Verify all simpoints are generated properly ...')
  simpoints = read_all_simpoints()
  print('Create checkpoints for all benchmarks ...')
  cmds = setup_checkpoint_dirs_and_get_create_commands(simpoints)
  command.launch(get_submission_system(), cmds)

def get_all_checkpoint_paths(workload_path, workload_name):
  glob_search_path = '{}/{}_checkpoint*'.format(workload_path, workload_name)
  checkpoint_paths = glob.glob(glob_search_path)
  if len(checkpoint_paths) == 0:
    print('ERROR: could not find any chekpoints in {} with glob search'
          ' path {}'.format(workload_path, glob_search_path))
    sys.exit(1)
  return checkpoint_paths 

def parse_checkpoint_path(checkpoint_path):
  m = re.match(r'.*_checkpoint(\d+)_([\.\d]+)_[\d]+', os.path.basename(checkpoint_path))
  if m is None:
    print('Checkpoint name in {} is ill-formed'.format(checkpoint_path))
  return int(m.group(1)), float(m.group(2))

def verify_checkpoints_are_sane(checkpoint_paths):
  total_weights = 0.0
  for checkpoint_path in checkpoint_paths:
    _, weight = parse_checkpoint_path(checkpoint_path)
    total_weights += weight

  if total_weights < 0.99:
    print('The weights of identified checkpoints do not add up to 1 for ')
    print('Identified checkpoints:')
    for path in checkpoint_paths:
      print(path)
    sys.exit(1)

def get_descriptor_definitions(benchmark, benchmark_name, checkpoint_paths):
  _info = []

  for checkpoint_path in checkpoint_paths:
    checkpoint_num, weight = parse_checkpoint_path(checkpoint_path)
    _info.append((checkpoint_path, checkpoint_num, weight,))

  definitions = ''

  checkpoint_name_list = []
  for path, checkpoint_num, weight in _info:
    name = '{}_ckpt{}'.format(benchmark_name, checkpoint_num)
    checkpoint_name_list.append(name)
    definitions += ('{name} = Checkpoint("{name}", "{path}", scarab_args='
                    '"--inst_limit {num_instructions} ", '
                    'weight={weight})\n'.format(
        name=name,
        path=path,
        num_instructions=__args__.simpoint_length,
        weight=weight))

  definitions += '{name} = Benchmark("{name}", ['.format(name=benchmark_name)
  for checkpoint_name in checkpoint_name_list:
    definitions += checkpoint_name + ', '
  definitions += '])\n\n'

  return definitions

def create_descriptor_file_phase():
  print('================================================')
  print('Create a desctiptor file for the checkpoints ...')

  descriptor_str = 'import os\n\n'
  name_list = []
  for benchmark in __benchmarks__:
    workload_path = os.path.abspath(benchmark._results_dir(__args__.output_dir) if benchmark.copy else benchmark.path)
    workload_parent = Path(workload_path).parent
    checkpoint_paths = get_all_checkpoint_paths(workload_parent, benchmark.name)
    verify_checkpoints_are_sane(checkpoint_paths)
    benchmarks_descriptor_str = get_descriptor_definitions(
        benchmark, benchmark.name, checkpoint_paths)
    descriptor_str += benchmarks_descriptor_str
    name_list.append(benchmark.name)

  with open(__args__.output_dir + '/descriptor.def', 'w') as f:
    f.write(descriptor_str)
    f.write('{suite} = Suite("{suite}", ['.format(suite="the_suite"))
    for name in name_list:
      f.write(name + ', ')
    f.write('])\n')

def main():
  initialize_globals()

  if not any([__args__.run_simpoints, __args__.create_checkpoints, __args__.create_descriptor_file]):
    run_all_phases = True
  else:
    run_all_phases = False

  if __args__.run_simpoints or run_all_phases:
    run_simpoints_phase()

  if __args__.create_checkpoints or run_all_phases:
    create_checkpoints_phase()

  if __args__.create_descriptor_file or run_all_phases:
    create_descriptor_file_phase()


if __name__ == '__main__':
  main()
