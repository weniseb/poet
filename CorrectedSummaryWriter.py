import torch
from torch.utils.tensorboard import SummaryWriter
from torch.utils.tensorboard.summary import hparams


class CorrectedSummaryWriter(SummaryWriter):
  def __init__(
      self,
      log_dir=None,
      comment="",
      **kwargs
  ):
    super().__init__(log_dir=log_dir, comment=comment, **kwargs)


  def add_hparams(self, hparam_dict, metric_dict, hparam_domain_discrete=None, run_name=None, global_step=None):
    torch._C._log_api_usage_once("tensorboard.logging.add_hparams")
    if type(hparam_dict) is not dict or type(metric_dict) is not dict:
      raise TypeError('hparam_dict and metric_dict should be dictionary.')
    exp, ssi, sei = hparams(hparam_dict, metric_dict)

    logdir = self._get_file_writer().get_logdir()
    with SummaryWriter(log_dir=logdir) as w_hp:
      w_hp.file_writer.add_summary(exp)
      w_hp.file_writer.add_summary(ssi)
      w_hp.file_writer.add_summary(sei)
      for k, v in metric_dict.items():
        w_hp.add_scalar(k, v)
