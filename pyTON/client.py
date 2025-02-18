# -*- coding: utf-8 -*-
import asyncio
import codecs
import struct
import socket
from concurrent.futures import ThreadPoolExecutor
import threading
from datetime import datetime, timezone
import time

import json
from .tonlibjson import TonLib
from .address_utils import prepare_address
from tvm_valuetypes import serialize_tvm_stack, render_tvm_stack, deserialize_boc


def b64str_bytes(b64str):
    b64bytes = codecs.encode(b64str, "utf8")
    return codecs.decode(b64bytes, "base64")


def b64str_str(b64str):
    _bytes = b64str_bytes(b64str)
    return codecs.decode(_bytes, "utf8")


def b64str_hex(b64str):
    _bytes = b64str_bytes(b64str)
    _hex = codecs.encode(_bytes, "hex")
    return codecs.decode(_hex, "utf8")


def h2b64(x):
    return codecs.encode(codecs.decode(x, 'hex'), 'base64').decode().replace("\n", "")


class TonlibClient:

    def __init__(self, loop, config, keystore, libtonlibjson):
        self.loop = loop
        self.config = config
        self.keystore = keystore
        self.libtonlibjson = libtonlibjson

    async def connect(self):
        pass

    async def reconnect(self):
        if not self.tonlib_wrapper.shutdown_state:
            print(time.time(), "reconnect")
            self.tonlib_wrapper.shutdown_state = "started"
            await self.init_tonlib()

    async def init_tonlib(self):
        """
        TL Spec
            init options:options = options.Info;
            options config:config keystore_type:KeyStoreType = Options;

            keyStoreTypeDirectory directory:string = KeyStoreType;
            config config:string blockchain_name:string use_callbacks_for_network:Bool ignore_cache:Bool = Config;

        :param ip: IPv4 address in dotted notation or signed int32
        :param port: IPv4 TCP port
        :param key: base64 pub key of liteserver node
        :return: None
        """
        self.loaded_contracts_num = 0
        wrapper = TonLib(self.loop, self.libtonlibjson)
        keystore_obj = {
            '@type': 'keyStoreTypeDirectory',
            'directory': self.keystore
        }
        request = {
            '@type': 'init',
            'options': {
                '@type': 'options',
                'config': {
                    '@type': 'config',
                    'config': json.dumps(self.config),
                    'use_callbacks_for_network': False,
                    'blockchain_name': '',
                    'ignore_cache': False
                },
                'keystore_type': keystore_obj
            }
        }

        await wrapper.execute(request)
        wrapper.set_restart_hook(hook=self.reconnect, max_requests=500)
        self.tonlib_wrapper = wrapper
        await self.set_verbosity_level(0)

    async def set_verbosity_level(self, level):
        request = {
            '@type': 'setLogVerbosityLevel',
            'new_verbosity_level': level
        }
        return await self.tonlib_wrapper.execute(request)

    async def raw_get_transactions(self, account_address: str, from_transaction_lt: str, from_transaction_hash: str):
        """
        TL Spec:
            raw.getTransactions account_address:accountAddress from_transaction_id:internal.transactionId = raw.Transactions;
            accountAddress account_address:string = AccountAddress;
            internal.transactionId lt:int64 hash:bytes = internal.TransactionId;
        :param account_address: str with raw or user friendly address
        :param from_transaction_lt: from transaction lt
        :param from_transaction_hash: from transaction hash in HEX representation
        :return: dict as
            {
                '@type': 'raw.transactions',
                'transactions': list[dict as {
                    '@type': 'raw.transaction',
                    'utime': int,
                    'data': str,
                    'transaction_id': internal.transactionId,
                    'fee': str,
                    'in_msg': dict as {
                        '@type': 'raw.message',
                        'source': str,
                        'destination': str,
                        'value': str,
                        'message': str
                    },
                    'out_msgs': list[dict as raw.message]
                }],
                'previous_transaction_id': internal.transactionId
            }
        """
        account_address = prepare_address(account_address)
        from_transaction_hash = h2b64(from_transaction_hash)

        request = {
            '@type': 'raw.getTransactions',
            'account_address': {
                'account_address': account_address,
            },
            'from_transaction_id': {
                '@type': 'internal.transactionId',
                'lt': from_transaction_lt,
                'hash': from_transaction_hash
            }
        }
        return await self.tonlib_wrapper.execute(request)

    async def get_transactions(self, account_address, from_transaction_lt=None, from_transaction_hash=None,
                               to_transaction_lt=0, limit=10):
        """
         Return all transactions between from_transaction_lt and to_transaction_lt
         if to_transaction_lt and to_transaction_hash are not defined returns all transactions
         if from_transaction_lt and from_transaction_hash are not defined checks last
        """
        if (from_transaction_lt == None) or (from_transaction_hash == None):
            addr = await self.raw_get_account_state(account_address)
            if '@type' in addr and addr['@type'] == "error":
                addr = await self.raw_get_account_state(account_address)
            if '@type' in addr and addr['@type'] == "error":
                raise Exception(addr["message"])
            try:
                from_transaction_lt, from_transaction_hash = int(addr["last_transaction_id"]["lt"]), b64str_hex(
                    addr["last_transaction_id"]["hash"])
            except KeyError:
                raise Exception("Can't get last_transaction_id data")
        reach_lt = False
        all_transactions = []
        current_lt, curret_hash = from_transaction_lt, from_transaction_hash
        while (not reach_lt) and (len(all_transactions) < limit):
            raw_transactions = await self.raw_get_transactions(account_address, current_lt, curret_hash)
            if (raw_transactions['@type']) == 'error':
                break
                # TODO probably we should chenge get_transactions API
                # if 'message' in raw_transactions['message']:
                #  raise Exception(raw_transactions['message'])
                # else:
                #  raise Exception("Can't get transactions")
            transactions, next = raw_transactions['transactions'], raw_transactions.get("previous_transaction_id", None)
            for t in transactions:
                tlt = int(t['transaction_id']['lt'])
                if tlt <= to_transaction_lt:
                    reach_lt = True
                    break
                all_transactions.append(t)
            if next:
                current_lt, curret_hash = int(next["lt"]), b64str_hex(next["hash"])
            else:
                break
            if current_lt == 0:
                break
        for t in all_transactions:
            try:
                if "in_msg" in t:
                    if "source" in t["in_msg"]:
                        t["in_msg"]["source"] = t["in_msg"]["source"]["account_address"]
                    if "destination" in t["in_msg"]:
                        t["in_msg"]["destination"] = t["in_msg"]["destination"]["account_address"]
                    try:
                        if "msg_data" in t["in_msg"]:
                            msg_cell_boc = codecs.decode(codecs.encode(t["in_msg"]["msg_data"]["body"], 'utf8'),
                                                         'base64')
                            message_cell = deserialize_boc(msg_cell_boc)
                            msg = message_cell.data.data.tobytes()
                            t["in_msg"]["message"] = codecs.decode(codecs.encode(msg, 'base64'), "utf8")
                    except:
                        t["in_msg"]["message"] = ""
                if "out_msgs" in t:
                    for o in t["out_msgs"]:
                        if "source" in o:
                            o["source"] = o["source"]["account_address"]
                        if "destination" in o:
                            o["destination"] = o["destination"]["account_address"]
                        try:
                            if "msg_data" in o:
                                msg_cell_boc = codecs.decode(codecs.encode(o["msg_data"]["body"], 'utf8'), 'base64')
                                message_cell = deserialize_boc(msg_cell_boc)
                                msg = message_cell.data.data.tobytes()
                                o["message"] = codecs.decode(codecs.encode(msg, 'base64'), "utf8")
                        except:
                            o["message"] = ""
            except Exception as e:
                print("getTransaction exception", e)
        return all_transactions

    async def raw_get_account_state(self, address: str):
        """
        TL Spec:
            raw.getAccountState account_address:accountAddress = raw.AccountState;
            accountAddress account_address:string = AccountAddress;
        :param address: str with raw or user friendly address
        :return: dict as
            {
                '@type': 'raw.accountState',
                'balance': str,
                'code': str,
                'data': str,
                'last_transaction_id': internal.transactionId,
                'sync_utime': int
            }
        """
        account_address = prepare_address(address)

        request = {
            '@type': 'raw.getAccountState',
            'account_address': {
                'account_address': address
            }
        }

        return await self.tonlib_wrapper.execute(request)

    async def generic_get_account_state(self, address: str):
        account_address = prepare_address(address)
        request = {
            '@type': 'generic.getAccountState',
            'account_address': {
                'account_address': address
            }
        }
        return await self.tonlib_wrapper.execute(request)

    async def _load_contract(self, address):
        account_address = prepare_address(address)
        request = {
            '@type': 'smc.load',
            'account_address': {
                'account_address': address
            }
        }
        r = await self.tonlib_wrapper.execute(request)
        self.loaded_contracts_num += 1
        return r["id"]

    async def raw_run_method(self, address, method, stack_data, output_layout=None):
        """
          For numeric data only
          TL Spec:
            smc.runGetMethod id:int53 method:smc.MethodId stack:vector<tvm.StackEntry> = smc.RunResult;

          smc.methodIdNumber number:int32 = smc.MethodId;
          smc.methodIdName name:string = smc.MethodId;

          tvm.slice bytes:string = tvm.Slice;
          tvm.cell bytes:string = tvm.Cell;
          tvm.numberDecimal number:string = tvm.Number;
          tvm.tuple elements:vector<tvm.StackEntry> = tvm.Tuple;
          tvm.list elements:vector<tvm.StackEntry> = tvm.List;

          tvm.stackEntrySlice slice:tvm.slice = tvm.StackEntry;
          tvm.stackEntryCell cell:tvm.cell = tvm.StackEntry;
          tvm.stackEntryNumber number:tvm.Number = tvm.StackEntry;
          tvm.stackEntryTuple tuple:tvm.Tuple = tvm.StackEntry;
          tvm.stackEntryList list:tvm.List = tvm.StackEntry;
          tvm.stackEntryUnsupported = tvm.StackEntry;

          smc.runResult gas_used:int53 stack:vector<tvm.StackEntry> exit_code:int32 = smc.RunResult;
        """
        stack_data = render_tvm_stack(stack_data)
        if isinstance(method, int):
            method = {'@type': 'smc.methodIdNumber', 'number': method}
        else:
            method = {'@type': 'smc.methodIdName', 'name': str(method)}
        contract_id = await self._load_contract(address);
        request = {
            '@type': 'smc.runGetMethod',
            'id': contract_id,
            'method': method,
            'stack': stack_data
        }
        r = await self.tonlib_wrapper.execute(request)
        if 'stack' in r:
            r['stack'] = serialize_tvm_stack(r['stack'])
        if '@type' in r and r['@type'] == 'smc.runResult':
            r.pop('@type')
        return r

    async def raw_send_message(self, serialized_boc):
        """
          raw.sendMessage body:bytes = Ok;

          :param serialized_boc: bytes, serialized bag of cell
        """
        serialized_boc = codecs.decode(codecs.encode(serialized_boc, "base64"), 'utf-8').replace("\n", '')
        request = {
            '@type': 'raw.sendMessage',
            'body': serialized_boc
        }
        return await self.tonlib_wrapper.execute(request)

    async def _raw_create_query(self, destination, body, init_code=b'', init_data=b''):
        """
          raw.createQuery destination:accountAddress init_code:bytes init_data:bytes body:bytes = query.Info;

          query.info id:int53 valid_until:int53 body_hash:bytes  = query.Info;

        """
        init_code = codecs.decode(codecs.encode(init_code, "base64"), 'utf-8').replace("\n", '')
        init_data = codecs.decode(codecs.encode(init_data, "base64"), 'utf-8').replace("\n", '')
        body = codecs.decode(codecs.encode(body, "base64"), 'utf-8').replace("\n", '')
        destination = prepare_address(destination)
        request = {
            '@type': 'raw.createQuery',
            'body': body,
            'init_code': init_code,
            'init_data': init_data,
            'destination': {
                'account_address': destination
            }
        }
        return await self.tonlib_wrapper.execute(request)

    async def _raw_send_query(self, query_info):
        """
          query.send id:int53 = Ok;
        """
        request = {
            '@type': 'query.send',
            'id': query_info['id']
        }
        return await self.tonlib_wrapper.execute(request)

    async def raw_create_and_send_query(self, destination, body, init_code=b'', init_data=b''):
        query_info = await self._raw_create_query(destination, body, init_code, init_data)
        return self._raw_send_query(query_info)

    async def raw_create_and_send_message(self, destination, body, initial_account_state=b''):
        # Very close to raw_create_and_send_query, but StateInit should be generated outside
        """
          raw.createAndSendMessage destination:accountAddress initial_account_state:bytes data:bytes = Ok;

        """
        initial_account_state = codecs.decode(codecs.encode(initial_account_state, "base64"), 'utf-8').replace("\n", '')
        body = codecs.decode(codecs.encode(body, "base64"), 'utf-8').replace("\n", '')
        destination = prepare_address(destination)
        request = {
            '@type': 'raw.createAndSendMessage',
            'destination': {
                'account_address': destination
            },
            'initial_account_state': initial_account_state,
            'data': body
        }
        return await self.tonlib_wrapper.execute(request)

    async def raw_estimate_fees(self, destination, body, init_code=b'', init_data=b'', ignore_chksig=True):
        query_info = await self._raw_create_query(destination, body, init_code, init_data)
        request = {
            '@type': 'query.estimateFees',
            'id': query_info['id'],
            'ignore_chksig': ignore_chksig
        }
        return await self.tonlib_wrapper.execute(request)

    async def getMasterchainInfo(self):
        request = {
            '@type': 'blocks.getMasterchainInfo'
        }
        return await self.tonlib_wrapper.execute(request)

    async def lookupBlock(self, workchain, shard, seqno=None, lt=None, unixtime=None):
        assert seqno or lt or unixtime, "Seqno, LT or unixtime should be defined"
        mode = 0
        if seqno:
            mode += 1
        if lt:
            mode += 2
        if unixtime:
            mode += 3
        request = {
            '@type': 'blocks.lookupBlock',
            'mode': mode,
            'id': {
                '@type': 'ton.blockId',
                'workchain': workchain,
                'shard': shard,
                'seqno': seqno
            },
            'lt': lt,
            'utime': unixtime
        }
        return await self.tonlib_wrapper.execute(request)

    async def getShards(self, master_seqno):
        wc, shard = -1, -9223372036854775808
        fullblock = await self.lookupBlock(wc, shard, master_seqno)
        request = {
            '@type': 'blocks.getShards',
            'id': fullblock
        }
        return await self.tonlib_wrapper.execute(request)

    async def raw_getBlockTransactions(self, fullblock, count, after_tx):
        request = {
            '@type': 'blocks.getTransactions',
            'id': fullblock,
            'mode': 7 if not after_tx else 7 + 128,
            'count': count,
            'after': after_tx
        }
        return await self.tonlib_wrapper.execute(request)

    async def getBlockTransactions(self, workchain, shard, seqno, root_hash=None, file_hash=None, count=None,
                                   after_lt=None, after_hash=None):
        fullblock = {}
        if root_hash and file_hash:
            fullblock = {
                '@type': 'ton.blockIdExt',
                'workchain': workchain,
                'shard': shard,
                'seqno': seqno,
                'root_hash': root_hash,
                'file_hash': file_hash
            }
        else:
            fullblock = await self.lookupBlock(workchain, shard, seqno)
            if fullblock.get('@type', 'error') == 'error':
                return fullblock
        after_tx = {
            '@type': 'blocks.accountTransactionId',
            'account': after_hash if after_hash else 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
            'lt': after_lt if after_lt else 0
        }
        total_result = None
        incomplete = True
        req_count = count if count else 40
        while incomplete:
            result = await self.raw_getBlockTransactions(fullblock, req_count, after_tx)
            if not total_result:
                total_result = result
            else:
                total_result["transactions"] += result["transactions"]
                total_result["incomplete"] = result["incomplete"]
            incomplete = result["incomplete"]
            if incomplete:
                after_tx['account'] = result["transactions"][-1]["account"]
                after_tx['lt'] = result["transactions"][-1]["lt"]
        # TODO automatically check incompleteness and download all txes
        for tx in total_result["transactions"]:
            try:
                tx["account"] = "%d:%s" % (result["id"]["workchain"], b64str_hex(tx["account"]))
            except:
                pass
        return total_result

    async def getBlockHeader(self, workchain, shard, seqno, root_hash=None, file_hash=None):
        if root_hash and file_hash:
            fullblock = {
                '@type': 'ton.blockIdExt',
                'workchain': workchain,
                'shard': shard,
                'seqno': seqno,
                'root_hash': root_hash,
                'file_hash': file_hash
            }
        else:
            fullblock = await self.lookupBlock(workchain, shard, seqno)
            if fullblock.get('@type', 'error') == 'error':
                return fullblock
        request = {
            '@type': 'blocks.getBlockHeader',
            'id': fullblock
        }
        return await self.tonlib_wrapper.execute(request)
