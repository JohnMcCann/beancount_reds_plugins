""" Opens a set of accounts based on rules. See accompanying README.md """

# flake8: noqa
import re
import time
from ast import literal_eval
from beancount.core import data
from beancount.core.data import Open
# from beancount.parser import printer

DEBUG = 0
__plugins__ = ('opengroup',)


default_rules = {
  'cash_and_fees': (  # Open cash and fees accounts
    '(?P<root>.*):(?P<subroot>.*):(?P<taxability>.*):(?P<account_name>.*)',

    [('{f_acct}:{f_ticker}', '{f_opcurr}'),
     ('Expenses:Fees-and-Charges:Brokerage-Fees:{taxability}:{account_name}', '{f_opcurr}'),
    ]),

  'commodity_leaves': (  # Open common set of investment accounts with commodity leaves
    '(?P<root>.*):(?P<subroot>.*):(?P<taxability>.*):(?P<account_name>.*)',

    [('Income:{subroot}:{taxability}:Dividends:{account_name}:{f_ticker}',     '{f_opcurr}'),
     ('Income:{subroot}:{taxability}:Interest:{account_name}:{f_ticker}',      '{f_opcurr}'),
     ('Income:{subroot}:{taxability}:Capital-Gains:{account_name}:{f_ticker}', '{f_opcurr}'),
    ]),

  'commodity_leaves_default_booking': (  # Open commodity_leaves + asset account for the ticker
    '(?P<root>.*):(?P<subroot>.*):(?P<taxability>.*):(?P<account_name>.*)',

    [('{f_acct}:{f_ticker}',                                                   '{f_ticker}'),
     ('Income:{subroot}:{taxability}:Dividends:{account_name}:{f_ticker}',     '{f_opcurr}'),
     ('Income:{subroot}:{taxability}:Interest:{account_name}:{f_ticker}',      '{f_opcurr}'),
     ('Income:{subroot}:{taxability}:Capital-Gains:{account_name}:{f_ticker}', '{f_opcurr}'),
    ]),

  'commodity_leaves_cgdists':  # Open capital gains distributions accounts
    ('(?P<root>.*):(?P<subroot>.*):(?P<taxability>.*):(?P<account_name>.*)',

    [('Income:{subroot}:{taxability}:Capital-Gains-Distributions:Long:{account_name}:{f_ticker}',  '{f_opcurr}'),
     ('Income:{subroot}:{taxability}:Capital-Gains-Distributions:Short:{account_name}:{f_ticker}', '{f_opcurr}'),
    ]),
}  # type: ignore


def run_rule(rules, rulename, f_acct, f_ticker, f_opcurr):
    rule, inserts = rules[rulename]
    components = re.search(rule, f_acct).groupdict()
    components.update(locals())

    def f(x):
        return x.format(**components)
    return [(f(i), f(currency).split(','), None) for i, currency in inserts]


def opengroup(entries, options_map, config):
    """Insert open entries based on rules.

    Args:
      entries: a list of entry instances
      options_map: a dict of options parsed from the file (not used)
      config: rules dictionary in the format of default_rules above
    Returns:
      A tuple of entries and errors. """

    start_time = time.time()
    close_count = 0
    new_entries = []
    errors = []

    opens = [e for e in entries if isinstance(e, Open)]
    # TODO: need to make this specifiable by the metadata param
    op_currency = options_map.get('operating_currency', [])
    if isinstance(op_currency, list) and len(op_currency):
        op_currency = op_currency[0]
    else:
        op_currency = 'USD'

    rules = literal_eval(config)
    if not rules:
        rules = default_rules

    for entry in opens:
        for m in entry.meta:
            if 'opengroup_' in m:
                rule = m[10:]
                # Insert open entries
                for leaf in entry.meta[m].split(","):
                    for acc_params in run_rule(rules, rule, entry.account, leaf, op_currency):
                        meta = data.new_metadata(entry.meta["filename"], entry.meta["lineno"])
                        new_entries.append(data.Open(meta, entry.date, *acc_params))
                        # printer.print_entry(data.Open(meta, entry.date, *acc_params))

    retval = entries + new_entries

    if DEBUG:
        elapsed_time = time.time() - start_time
        print("Close account tree [{:.2f}s]: {} close entries added.".format(elapsed_time, close_count))

    return retval, errors
