"""Plugin for accounts that should sum up to zero. Determines transactions
that when taken together, sum up to zero, and move them to a specified
account. The remaining entries are the 'unmatched' ones, that need attention
from the user.

Motivation:
-----------

Real-world transfers frequently occur between accounts. For example, between a
checking account and an investment account. When double entry bookkeeping is
used to track such transfers, we end up with two problems:

    a) when account statements are converted to double-entry format, the user
    has to manually match the transfers on account statements from the two
    institutions involved, and remove one of the entries since they are
    redundant.

    b) even when (a) is done, the transfer might take a day or more to
    complete: the two accounts involved would then reflect the transfer on
    different dates.

Since the money is truly missing from all the physical accounts for the period
of transfer, they can be accounted for as shown in this example:

2005-01-01 Transfer
  Assets:Bank_of_Ameriplus  -20 USD
  ZeroSumAccount:Transfers

2005-01-03 Transfer
  Assets:TB_Trading  20 USD
  ZeroSumAccount:Transfers

Doing so has a few advantages:

    a) on 2005-01-02, your assets are accurately represented:
    Bank_of_Ameriplus is short by $20, TB_Trading still doesn't have it, and
    the ZeroSumAccount:Transfers account captures that the money is still
    yours, but is "in flight."

    b) One can convert each bank's transactions directly into double-entry
    ledger statements. No need to remove the transaction from one of the
    banks. When you look at your journal files for each account, they match
    your account statements exactly.

    c) Import/conversion (from say, a bank .csv or .ofx) is easier, because
    your import scripts don't have to figure out where a transfer goes, and
    can simply assign transfers to  ZeroSumAccount:Transfers

    d) If there is a problem, your ZeroSumAccount:Transfers will sum to a
    non-zero value. Errors can therefore be found easily.


What this plugin does:
----------------------

Account statements from institutions can be directly converted to double-entry
format, with transfers simply going to a special transfers account (eg:
Assets:ZeroSumAccount:Transfers).

In this plugin, we identify sets of postings in the specified ZeroSum accounts
that sum up to zero, and move them to a specified target account. This target
account will always sum up to zero and needs no further attention. The
postings remaining in the original ZeroSum accounts were the ones that could
not be matched, and potentially need attention.

The plugin operates on postings (not transactions) in the ZeroSum accounts.
This way, transactions with multiple postings to a ZeroSum account are still
matched without special handling.

The following examples will be matched and moved by this plugin:

    Example 1:
    ----------
    Input:
        2005-01-01 Transfer
          Assets:Bank_of_Ameriplus  -20 USD
          ZeroSumAccount:Transfers

        2005-01-03 Transfer
          Assets:TB_Trading  20 USD
          ZeroSumAccount:Transfers
    Output:
        2005-01-01 Transfer
          Assets:Bank_of_Ameriplus  -20 USD
          ZeroSumAccount-Matched:Transfers

        2005-01-03 Transfer
          Assets:TB_Trading  20 USD
          ZeroSumAccount-Matched:Transfers

    Example 2 (Only input shown):
    -----------------------------
    2005-01-01 Transfer
      Assets:Bank_of_Ameriplus  -20 USD
      ZeroSumAccount:Transfers   10 USD
      ZeroSumAccount:Transfers   10 USD

    2005-01-03 Transfer
      Assets:TB_Trading_A  10 USD
      ZeroSumAccount:Transfers

    2005-01-04 Transfer
      Assets:TB_Trading_B  10 USD
      ZeroSumAccount:Transfers

The following examples will NOT be matched:

    Example A:
    ----------
    2005-01-01 Transfer
      Assets:Bank_of_Ameriplus  -20 USD
      ZeroSumAccount:Transfers   10 USD
      ZeroSumAccount:Transfers   10 USD

    2005-01-03 Transfer
      Assets:TB_Trading  20 USD
      ZeroSumAccount:Transfers

    Example B:
    ----------
    2005-01-01 Transfer
      Assets:Bank_of_Ameriplus  -20 USD
      ZeroSumAccount:Transfers

    2005-01-03 Transfer
      Assets:TB_Trading_A  10 USD
      ZeroSumAccount:Transfers

    2005-01-03 Transfer
      Assets:TB_Trading_B  10 USD
      ZeroSumAccount:Transfers


The plugin does not append/remove the original set of input transaction
entries. It only changes the accounts to which postings are made. The plugin
also automatically adds "Open" directives for the target accounts to which
matched transactions are moved.

Invoking the plugin:
--------------------
First, an example:

    plugin "beancount.plugins.zerosum" "{
     'zerosum_accounts' : {
     'Assets:Zero-Sum-Accounts:Bank-Account-Transfers' : ('Assets:ZSA-Matched:Bank-Account-Transfers', 30),
     'Assets:Zero-Sum-Accounts:Credit-Card-Payments'   : ('Assets:ZSA-Matched:Credit-Card-Payments'  ,  6),
     'Assets:Zero-Sum-Accounts:Temporary'              : ('Assets:ZSA-Matched:Temporary'             , 90),
      }
     }"

As the example shows, the argument is a dictionary where the keys are the set
of accounts on which the plugin should operate. The values are
(target_account, date_range), where the target_account is the account to which
the plugin should move matched postings, and the date_range is the range over
which to check for matches for that account.

TODO:
- allow config using account metadata
- take plugin params from metadata (including date_range)
- optionally create a linking metadata (or a beancount-link) between matches

"""

import datetime
import random
import string
import time

from ast import literal_eval
from collections import defaultdict

from beancount.core import data
from beancount.core import flags
from beancount_reds_plugins.common import common

DEBUG = 0
DEFAULT_TOLERANCE = 0.0099
MATCHING_ID_STRING = "match_id"
random.seed(6)  # arbitrary fixed seed

__plugins__ = ('zerosum', 'flag_unmatched',)


# replace the account on a given posting with a new account
def account_replace(txn, posting, new_account, match_id):
    # create a new posting with the new account, then remove old and add new
    # from parent transaction
    if match_id:
        if posting.meta:
            # Will overwrite an existing match (shouldn't exist)
            posting.meta.update({MATCHING_ID_STRING: match_id})
        else:
            posting.meta = {MATCHING_ID_STRING: match_id}
    new_posting = posting._replace(account=new_account)
    txn.postings.remove(posting)
    txn.postings.append(new_posting)


def zerosum(entries, options_map, config):  # noqa: C901
    """Insert entries for unmatched transactions in zero-sum accounts.

    Args:
      entries: a list of entry instances

      options_map: a dict of options parsed from the file (not used)

      config: Python dict with two entries:

      - 'zerosum_accounts': maps zerosum_account_name -> (matched_zerosum_account_name,
        date_range). matched_zerosum_account_name is optional, and can be left blank. If
        left blank, the name of the matched account is derived from the
        zerosum_account_name, by performing the string replacement specified by
        'account_name_replace' (see below)

      - 'account_name_replace': tuple of two entries. See above

      - 'tolerance': the maximum cost difference between two matching postings

      - 'flag_unmatched': bool to control whether to flag unmatched
        transactions as warnings (default off)

      - 'match_metadata': bool to control whether matched postings have metadata
        linking the matched transactions, allowing manual verification in post.

      See example for more info.

    Returns:
      A tuple of entries and errors.

    """

    def find_match():
        '''Look forward to find a match, until date range is exceeded'''
        max_date = txn.date + datetime.timedelta(days=date_range)

        for j in range(i, len(zerosum_txns)):
            t = zerosum_txns[j]
            if t.date > max_date:
                return None
            for p in t.postings:
                if p is posting:
                    # Don't match with the same exact posting.
                    continue
                if (abs(p.units.number + posting.units.number) < tolerance
                   and p.account == zs_account):
                    return (p, t)
        return None

    def generate_match_id():
        '''Generates a random string to be used as the match ID.'''
        return ''.join(
            random.choices(string.ascii_letters + string.digits, k=20))

    if DEBUG:
        # pr = cProfile.Profile()
        # pr.enable()
        start_time = time.time()

    config_obj = literal_eval(config)  # TODO: error check
    zs_accounts_list = config_obj.pop('zerosum_accounts', {})
    (account_name_from, account_name_to) = config_obj.pop('account_name_replace', ('', ''))
    tolerance = config_obj.pop('tolerance', DEFAULT_TOLERANCE)
    match_metadata = config_obj.pop('match_metadata', False)

    new_accounts = set()
    zerosum_postings_count = 0
    match_count = 0

    # Build zerosum_txns_all for all zs_accounts, so we iterate through entries only once (for performance)
    zerosum_txns_all = defaultdict(list)
    for entry in entries:
        if isinstance(entry, data.Transaction):
            for zs_account, _ in zs_accounts_list.items():
                if any(posting.account == zs_account for posting in entry.postings):
                    zerosum_txns_all[zs_account].append(entry)
                    zerosum_postings_count += 1
                    # count doesn't account for multiple matching postings, but is close enough

    for zs_account, (target_account, date_range) in zs_accounts_list.items():
        if not target_account:
            target_account = zs_account.replace(account_name_from, account_name_to)
        zerosum_txns = zerosum_txns_all[zs_account]

        # for each posting in each transaction, attempt to find a match. Replace account names in each each
        # matched posting pair
        for i in range(len(zerosum_txns)):
            txn = zerosum_txns[i]
            reprocess = True
            while reprocess:  # necessary since this entry's postings changes under us when we find a match
                for posting in txn.postings:
                    reprocess = False
                    if posting.account == zs_account:
                        match = find_match()
                        if match:
                            # print('Match:', txn.date, match[1].date, match[1].date - txn.date,
                            #         posting.units, posting.meta['lineno'], match[0].meta['lineno'])
                            match_count += 1
                            match_id = generate_match_id() if match_metadata else None
                            account_replace(txn,      posting,  target_account,
                                            match_id)
                            account_replace(match[1], match[0], target_account,
                                            match_id)
                            new_accounts.add(target_account)
                            reprocess = True
                            break

    new_open_entries = common.create_open_directives(new_accounts, entries, meta_desc='<zerosum>')

    if DEBUG:
        elapsed_time = time.time() - start_time
        print("Zerosum [{:.1f}s]: {}/{} postings matched from {} transactions. {} new accounts added.".format(
            elapsed_time, match_count*2, zerosum_postings_count, len(entries), len(new_open_entries)))
        # pr.disable()
        # pr.dump_stats('out.profile')

    return entries + new_open_entries, []


def flag_unmatched(entries, unused_options_map, config):
    '''Iterate again, to flag unmatched entries'''

    config_obj = literal_eval(config)
    if not config_obj.get('flag_unmatched'):
        return (entries, [])

    new_entries = []
    zs_accounts = config_obj['zerosum_accounts'].keys()
    for entry in entries:
        if isinstance(entry, data.Transaction):
            for posting in entry.postings:
                if posting.account in zs_accounts:
                    entry = entry._replace(flag=flags.FLAG_WARNING)
                    break
        new_entries.append(entry)
    return new_entries, []
