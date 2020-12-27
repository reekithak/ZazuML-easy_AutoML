import copy
import os
import sys
import time
from collections import OrderedDict, defaultdict
import yaml

import torch
import numpy as np
from hyperopt import hp
import ray
from ray.tune.suggest.hyperopt import HyperOptSearch
from ray.tune import register_trainable, run_experiments, run
from ray import tune
# sys.path.insert(1, os.path.dirname(os.path.dirname(__file__)))
# sys.path.insert(1, os.path.dirname(__file__))
from augmentations_tuner.fastautoaugment.FastAutoAugment.archive import remove_deplicates, policy_decoder
from dataloader import detection_augment_list
from augmentations_tuner.fastautoaugment.FastAutoAugment.common import get_logger, add_filehandler
from augmentations_tuner.fastautoaugment.FastAutoAugment.data import get_data
from networks import get_model, num_class
from augmentations_tuner.fastautoaugment.FastAutoAugment.train import train_and_eval
from theconf import Config as C, ConfigArgumentParser
import json
from pystopwatch2 import PyStopwatch
from easydict import EasyDict as edict
w = PyStopwatch()
from objectdetection.csv_eval import evaluate
top1_valid_by_cv = defaultdict(lambda: list)



logger = get_logger('Fast AutoAugment')


def _get_path(dataset, model, tag):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), 'FastAutoAugment',
                        'models/%s_%s_%s.pt' % (dataset, model, tag))  # TODO


# @ray.remote(num_gpus=4, max_calls=1) #TODO: change to num_gpus=1 ???
# @ray.remote
def train_model(config, dataroot, augment, cv_ratio_test, cv_fold, save_path=None, skip_exist=False):
    C.get()
    C.get().conf = config
    C.get().aug = augment

    result = train_and_eval(config, None, dataroot, cv_ratio_test, cv_fold, save_path=save_path, only_eval=skip_exist)
    return C.get()['model'], cv_fold, result


# def eval_tta(config, augment, reporter):
def eval_tta(config, augment):
    augment['num_policy'] = 1  # TODO remove
    C.get()
    C.get().conf = config
    cv_ratio_test, cv_fold, save_path = augment['cv_ratio_test'], augment['cv_fold'], augment['save_path']
    print(augment)
    # setup - provided augmentation rules
    C.get().aug = policy_decoder(augment, augment['num_policy'], augment['num_op'])

    # eval
    ckpt = torch.load(save_path)
    model = get_model(ckpt['model_specs']['name'], len(ckpt['labels']), ckpt['model_specs']['training_configs'], local_rank=ckpt['devices']['gpu_index']) #TODO: get model configuration from Retinanet

    if 'model' in ckpt:
        model.load_state_dict(ckpt['model'])
    else:
        model.load_state_dict(ckpt)
    model.eval()
    dataroot = os.path.join(augment['working_dir'], ckpt['model_specs']['data']['home_path'])
    mAPs = []
    start_t = time.time()
    for _ in range(augment['num_policy']):  # TODO
        train_dataset, test_dataset = get_data(ckpt['model_specs']['data']['annotation_type'], dataroot, augment,
                                                  split=cv_ratio_test, split_idx=cv_fold)
        mAP = evaluate(train_dataset, model) #TODO: adjust from train to testing on randomely selected perecentage every time
        mAPs.append(mAP)
        del train_dataset, test_dataset

    gpu_secs = (time.time() - start_t) * torch.cuda.device_count()
    tune.report(top1_valid=np.mean(mAPs), elapsed_time=gpu_secs)
    return np.mean(mAPs)

def augsearch(args=None, paths_ls=None):
    if args is None:
        d = yaml.load(open('/root/ZazuML/augmentations_tuner/fastautoaugment/confs/resnet50.yaml'), Loader=yaml.FullLoader)
        # from argparse import Namespace
        # args = Namespace(**d)
        args = edict(d)
    args.redis = 'gpu-cloud-vnode30.dakao.io:23655'
    args.per_class = True
    args.resume = True
    args.smoke_test = True

    if args.decay > 0:
        logger.info('decay=%.4f' % args.decay)
        args['optimizer']['decay'] = args.decay

    add_filehandler(logger, os.path.join('augmentations_tuner/fastautoaugment/FastAutoAugment/models', '%s_%s_cv%.1f.log' % (
        args['dataset'], args['model'], args.cv_ratio)))

    logger.info('initialize ray...')
    ray.init(num_cpus=1, num_gpus=1)

    num_result_per_cv = 10 if not args.smoke_test else 2
    cv_num = 5 if paths_ls is None else len(paths_ls)
    args.version = 1
    args._timestamp = '2020/08/30 20:40:10'
    args.config = '/home/noam/ZazuML/augmentations_tuner/fastautoaugment/confs/resnet50.yaml'

    copied_args = copy.deepcopy(args)
    copied_args = copied_args

    logger.info('search augmentation policies, dataset=%s model=%s' % (args['dataset'], args['model']))
    logger.info('----- Train without Augmentations ratio(test)=%.1f -----' % (args.cv_ratio))
    w.start(tag='train_no_aug')
    if paths_ls is None:
        paths_ls = [_get_path(args['dataset'], args['model'], 'ratio%.1f_fold%d' % (args.cv_ratio, i)) for i
                    in
                    range(cv_num)]
        print(paths_ls)
        logger.info('getting results...')
        pretrain_results = [
            train_model(copy.deepcopy(copied_args), args.dataroot, args['aug'], args.cv_ratio, i, save_path=paths_ls[i],
                        skip_exist=args.smoke_test)
            for i in range(cv_num)]

    logger.info('processed in %.4f secs' % w.pause('train_no_aug'))

    if args.until == 1:
        sys.exit(0)

    logger.info('----- Search Test-Time Augmentation Policies -----')
    w.start(tag='search')

    ops = detection_augment_list()
    space = {}
    for i in range(args.num_policy):
        for j in range(args.num_op):
            space['policy_%d_%d' % (i, j)] = hp.choice('policy_%d_%d' % (i, j), list(range(0, len(ops))))
            space['prob_%d_%d' % (i, j)] = hp.uniform('prob_%d_ %d' % (i, j), 0.0, 1.0)
            space['level_%d_%d' % (i, j)] = hp.uniform('level_%d_ %d' % (i, j), 0.0, 1.0)

    def eval_t(augs):
        print(augs)
        return eval_tta(copy.deepcopy(copied_args), augs)

    final_policy_set = []
    total_computation = 0
    reward_attr = 'top1_valid'  # top1_valid or minus_loss
    for _ in range(1):  # run multiple times.
        for cv_fold in range(cv_num):
            name = "search_%s_%s_fold%d_ratio%.1f" % (
                args['dataset'], args['model'], cv_fold, args.cv_ratio)
            print(name)
            algo = HyperOptSearch(space, max_concurrent=1, metric=reward_attr)
            aug_config = {
                'working_dir': os.getcwd(), 'save_path': paths_ls[cv_fold],
                'cv_ratio_test': args.cv_ratio, 'cv_fold': cv_fold,
                'num_op': args.num_op, 'num_policy': args.num_policy
            }
            num_samples = 4 if args.smoke_test else args.num_search
            print(aug_config)
            # eval_t(aug_config)
            results = run(eval_t, search_alg=algo, config=aug_config, num_samples=num_samples,
                          resources_per_trial={'gpu': 1}, stop={'training_iteration': args.num_policy})
            dataframe = results.dataframe().sort_values(reward_attr, ascending=False)
            total_computation = dataframe['elapsed_time'].sum()
            for i in range(num_result_per_cv):
                config_dict = dataframe.loc[i].filter(like='config').to_dict()
                new_keys = [x.replace('config/', '') for x in config_dict.keys()]
                new_config_dict = {}
                for key in new_keys:
                    new_config_dict[key] = config_dict['config/' + key]
                final_policy = policy_decoder(new_config_dict, args.num_policy, args.num_op)
                logger.info('top1_valid=%.4f %s' % (dataframe.loc[i]['top1_valid'].item(), final_policy))

                final_policy = remove_deplicates(final_policy)
                final_policy_set.extend(final_policy)

    logger.info(json.dumps(final_policy_set))
    logger.info('final_policy=%d' % len(final_policy_set))
    logger.info('processed in %.4f secs, gpu hours=%.4f' % (w.pause('search'), total_computation / 3600.))
    logger.info('----- Train with Augmentations model=%s dataset=%s aug=%s ratio(test)=%.1f -----' % (
        args['model'], args['dataset'], args.aug, args.cv_ratio))
    w.start(tag='train_aug')
    return final_policy_set


if __name__ == '__main__':

    augsearch = AugSearch()

