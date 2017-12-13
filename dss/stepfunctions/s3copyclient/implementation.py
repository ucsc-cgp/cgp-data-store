import binascii
import collections
import concurrent.futures as futures
import copy

import hashlib
import threading
import typing

import boto3
from cloud_blobstore.s3 import S3BlobStore

from ...stepfunctions import generator
from ...stepfunctions.lambdaexecutor import TimedThread
from ...util.aws import get_s3_chunk_size


# CONSTANTS
LAMBDA_PARALLELIZATION_FACTOR = 32
CONCURRENT_REQUESTS = 8


# Public input/output keys for the state object.
class Key:
    SOURCE_BUCKET = "srcbucket"
    SOURCE_KEY = "srckey"
    DESTINATION_BUCKET = "dstbucket"
    DESTINATION_KEY = "dstkey"
    FINISHED = "finished"


# Internal key for the state object.
class _Key:
    SOURCE_ETAG = "srcetag"
    UPLOAD_ID = "uploadid"
    SIZE = "size"
    PART_SIZE = "partsz"
    NEXT_PART = "next"
    LAST_PART = "last"
    PART_COUNT = "count"


def setup_copy_task(event, lambda_context):
    source_bucket = event[Key.SOURCE_BUCKET]
    source_key = event[Key.SOURCE_KEY]
    destination_bucket = event[Key.DESTINATION_BUCKET]
    destination_key = event[Key.DESTINATION_KEY]

    s3_blobstore = S3BlobStore()
    blobinfo = s3_blobstore.get_all_metadata(source_bucket, source_key)
    source_etag = blobinfo['ETag'].strip("\"")  # the ETag is returned with an extra set of quotes.
    source_size = blobinfo['ContentLength']  # type: int
    part_size = get_s3_chunk_size(source_size)
    part_count = source_size // part_size
    if part_count * part_size < source_size:
        part_count += 1
    if part_count > 1:
        mpu = s3_blobstore.s3_client.create_multipart_upload(Bucket=destination_bucket, Key=destination_key)
        upload_id = mpu['UploadId']
    else:
        upload_id = None

    event[_Key.SOURCE_ETAG] = source_etag
    event[_Key.UPLOAD_ID] = upload_id
    event[_Key.SIZE] = source_size
    event[_Key.PART_SIZE] = part_size
    event[_Key.PART_COUNT] = part_count
    event[Key.FINISHED] = False

    return event


def copy_worker(event, lambda_context, branch_id):
    class CopyWorkerTimedThread(TimedThread[dict]):
        def __init__(self, timeout_seconds: float, state: dict, slice_num: int) -> None:
            super().__init__(timeout_seconds, state)
            self.slice_num = slice_num

            self.source_bucket = state[Key.SOURCE_BUCKET]
            self.source_key = state[Key.SOURCE_KEY]
            self.source_etag = state[_Key.SOURCE_ETAG]
            self.destination_bucket = state[Key.DESTINATION_BUCKET]
            self.destination_key = state[Key.DESTINATION_KEY]
            self.upload_id = state[_Key.UPLOAD_ID]
            self.size = state[_Key.SIZE]
            self.part_size = state[_Key.PART_SIZE]
            self.part_count = state[_Key.PART_COUNT]

        def run(self) -> dict:
            s3_blobstore = S3BlobStore()
            state = self.get_state_copy()

            if _Key.NEXT_PART not in state or _Key.LAST_PART not in state:
                # missing the next/last part data.  calculate that from the branch id information.
                parts_per_branch = ((self.part_count + LAMBDA_PARALLELIZATION_FACTOR - 1) //
                                    LAMBDA_PARALLELIZATION_FACTOR)
                state[_Key.NEXT_PART] = self.slice_num * parts_per_branch + 1
                state[_Key.LAST_PART] = min(state[_Key.PART_COUNT], state[_Key.NEXT_PART] + parts_per_branch - 1)
                self.save_state(state)

            if state[_Key.NEXT_PART] > state[_Key.LAST_PART]:
                state[Key.FINISHED] = True
                return state

            queue = collections.deque(s3_blobstore.find_next_missing_parts(
                self.destination_bucket,
                self.destination_key,
                self.upload_id,
                self.part_count,
                state[_Key.NEXT_PART],
                state[_Key.LAST_PART] - state[_Key.NEXT_PART] + 1))

            state_lock = threading.Lock()

            def make_on_complete_callback(part_id: int):
                def callback():
                    with state_lock:
                        if part_id >= state[_Key.NEXT_PART]:
                            state[_Key.NEXT_PART] = part_id + 1
                            self.save_state(state)

                return callback

            with futures.ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as executor:
                for part_id in queue:
                    future = executor.submit(self.copy_one_part, part_id)
                    future.add_done_callback(make_on_complete_callback(part_id))

            state[Key.FINISHED] = True
            return state

        def copy_one_part(self, part_id: int):
            byte_range = self.calculate_range_for_part(part_id)
            s3_client = boto3.client("s3")
            s3_client.upload_part_copy(
                Bucket=self.destination_bucket,
                CopySource=dict(
                    Bucket=self.source_bucket,
                    Key=self.source_key,
                ),
                CopySourceIfMatch=self.source_etag,
                CopySourceRange=f"bytes={byte_range[0]}-{byte_range[1]}",
                Key=self.destination_key,
                PartNumber=part_id,
                UploadId=self.upload_id,
            )

        def calculate_range_for_part(self, part_id) -> typing.Tuple[int, int]:
            """Calculate the byte range for `part_id`.  Assume these are S3 part IDs, which are 1-indexed."""
            start = (part_id - 1) * self.part_size
            end = part_id * self.part_size
            if end >= self.size:
                end = self.size
            end -= 1

            return start, end

    slice_num = branch_id[-1]
    result = CopyWorkerTimedThread(lambda_context.get_remaining_time_in_millis() / 1000, event, slice_num).start()

    # because it would be comically large to have the full state for every worker, we strip the state if:
    #  1) we are finished
    #  2) we're not the 0th slice.

    if result[Key.FINISHED] and slice_num != 0:
        return {Key.FINISHED: True}
    else:
        return result


def join(event, lambda_context):
    # which parts are present?
    s3_resource = boto3.resource("s3")

    # only the 0th worker propagates the full state.
    state = event[0]

    mpu = s3_resource.MultipartUpload(
        state[Key.DESTINATION_BUCKET], state[Key.DESTINATION_KEY], state[_Key.UPLOAD_ID])

    parts = list(mpu.parts.all())

    assert len(parts) == state[_Key.PART_COUNT]

    # it's all present!
    parts_list = [dict(ETag=part.e_tag,
                       PartNumber=part.part_number)
                  for part in parts
                  ]

    # verify that the ETag of the output file will match the source etag.
    bin_md5 = b"".join([binascii.unhexlify(part.e_tag.strip("\""))
                        for part in parts])
    composite_etag = hashlib.md5(bin_md5).hexdigest() + "-" + str(len(parts))
    assert composite_etag == state[_Key.SOURCE_ETAG]

    mpu.complete(MultipartUpload=dict(Parts=parts_list))
    return state


retry_default = [
    {
        "ErrorEquals": ["States.Timeout", "States.TaskFailed"],
        "IntervalSeconds": 30,
        "MaxAttempts": 10,
        "BackoffRate": 1.618,
    },
]


threadpool_sfn = {
    "StartAt": "Worker{t}",
    "States": {
        "Worker{t}": {
            "Type": "Task",
            "Resource": copy_worker,
            "Next": "Branch{t}",
            "Retry": copy.deepcopy(retry_default),
        },
        "Branch{t}": {
            "Type": "Choice",
            "Choices": [{
                "Variable": "$.finished",
                "BooleanEquals": True,
                "Next": "EndThread{t}"
            }],
            "Default": "Worker{t}",
        },
        "EndThread{t}": {
            "Type": "Pass",
            "End": True,
        },
    }
}

sfn = {
    "StartAt": "SetupCopyTask",
    "States": {
        "SetupCopyTask": {
            "Type": "Task",
            "Resource": setup_copy_task,
            "Next": "Threadpool",
            "Retry": copy.deepcopy(retry_default),
        },
        "Threadpool": {
            "Type": "Parallel",
            "Branches": generator.ThreadPoolAnnotation(threadpool_sfn, LAMBDA_PARALLELIZATION_FACTOR, "{t}"),
            "Next": "Finalizer",
            "Retry": copy.deepcopy(retry_default),
        },
        "Finalizer": {
            "Type": "Task",
            "Resource": join,
            "End": True,
        },
    }
}
