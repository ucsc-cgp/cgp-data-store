#!/usr/bin/env python
# coding: utf-8

import datetime
import hashlib
import io
import nestedcontext
import os
import requests
import sys
import threading
import time
import typing
import unittest
from unittest import mock
import urllib.parse
import uuid
import json

pkg_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))  # noqa
sys.path.insert(0, pkg_root)  # noqa

from cloud_blobstore import BlobMetadataField
import dss
from dss.api.bundles import RETRY_AFTER_INTERVAL
from dss.config import BucketConfig, Config, override_bucket_config, Replica
from dss.util import UrlBuilder
from dss.storage.blobstore import test_object_exists
from dss.storage.hcablobstore import compose_blob_key
from dss.util.version import datetime_to_version_format
from dss.storage.bundles import get_bundle_manifest
from tests.infra import DSSAssertMixin, DSSUploadMixin, ExpectedErrorFields, get_env, testmode
from tests.infra.server import ThreadedLocalServer
from tests import eventually, get_auth_header


BUNDLE_GET_RETRY_COUNT = 60
"""For GET /bundles requests that require a retry, this is the maximum number of attempts we make."""


class TestBundleApi(unittest.TestCase, DSSAssertMixin, DSSUploadMixin):
    @classmethod
    def setUpClass(cls):
        cls.app = ThreadedLocalServer()
        cls.app.start()

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()

    def setUp(self):
        dss.Config.set_config(dss.BucketConfig.TEST)
        self.s3_test_bucket = get_env("DSS_S3_BUCKET_TEST")
        self.gs_test_bucket = get_env("DSS_GS_BUCKET_TEST")
        self.s3_test_fixtures_bucket = get_env("DSS_S3_BUCKET_TEST_FIXTURES")
        self.gs_test_fixtures_bucket = get_env("DSS_GS_BUCKET_TEST_FIXTURES")

    @testmode.integration
    def test_bundle_get(self):
        self._test_bundle_get(Replica.aws)
        self._test_bundle_get(Replica.gcp)

    def _test_bundle_get(self, replica: Replica):
        with self.subTest(replica):
            bundle_uuid = "011c7340-9b3c-4d62-bf49-090d79daf198"
            version = "2017-06-20T214506.766634Z"

            url = str(UrlBuilder()
                      .set(path="/v1/bundles/" + bundle_uuid)
                      .add_query("replica", replica.name)
                      .add_query("version", version))

            with override_bucket_config(BucketConfig.TEST_FIXTURE):
                resp_obj = self.assertGetResponse(
                    url,
                    requests.codes.ok)

            self.assertEqual(resp_obj.json['bundle']['uuid'], bundle_uuid)
            self.assertEqual(resp_obj.json['bundle']['version'], version)
            self.assertEqual(resp_obj.json['bundle']['creator_uid'], 12345)
            self.assertEqual(resp_obj.json['bundle']['files'][0]['content-type'], "text/plain")
            self.assertEqual(resp_obj.json['bundle']['files'][0]['size'], 11358)
            self.assertEqual(resp_obj.json['bundle']['files'][0]['crc32c'], "e16e07b9")
            self.assertEqual(resp_obj.json['bundle']['files'][0]['name'], "LICENSE")
            self.assertEqual(resp_obj.json['bundle']['files'][0]['s3_etag'], "3b83ef96387f14655fc854ddc3c6bd57")
            self.assertEqual(resp_obj.json['bundle']['files'][0]['sha1'], "2b8b815229aa8a61e483fb4ba0588b8b6c491890")
            self.assertEqual(resp_obj.json['bundle']['files'][0]['sha256'],
                             "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30")
            self.assertEqual(resp_obj.json['bundle']['files'][0]['uuid'], "ce55fd51-7833-469b-be0b-5da88ebebfcd")
            self.assertEqual(resp_obj.json['bundle']['files'][0]['version'], "2017-06-16T193604.240704Z")

    @testmode.integration
    def test_bundle_get_directaccess(self):
        self._test_bundle_get_directaccess(Replica.aws, True)
        self._test_bundle_get_directaccess(Replica.aws, False)
        self._test_bundle_get_directaccess(Replica.gcp, True)
        self._test_bundle_get_directaccess(Replica.gcp, False)

    def _test_bundle_get_directaccess(self, replica: Replica, explicit_version: bool):
        with self.subTest(f"{replica} {explicit_version}"):
            schema = replica.storage_schema

            bundle_uuid = "011c7340-9b3c-4d62-bf49-090d79daf198"
            version = "2017-06-20T214506.766634Z"

            url = UrlBuilder().set(path="/v1/bundles/" + bundle_uuid)
            url.add_query("replica", replica.name)
            url.add_query("directurls", "true")
            if explicit_version:
                url.add_query("version", version)

            with override_bucket_config(BucketConfig.TEST_FIXTURE):
                resp_obj = self.assertGetResponse(
                    str(url),
                    requests.codes.ok,
                    redirect_follow_retries=BUNDLE_GET_RETRY_COUNT,
                    min_retry_interval_header=RETRY_AFTER_INTERVAL,
                    override_retry_interval=1,
                )

            directaccess_url = resp_obj.json['bundle']['files'][0]['url']
            splitted = urllib.parse.urlparse(directaccess_url)
            self.assertEqual(splitted.scheme, schema)
            bucket = splitted.netloc
            key = splitted.path[1:]  # ignore the / part of the path.

            handle = Config.get_blobstore_handle(replica)
            contents = handle.get(bucket, key)

            hasher = hashlib.sha1()
            hasher.update(contents)
            sha1 = hasher.hexdigest()
            self.assertEqual(bucket, replica.checkout_bucket)
            self.assertEqual(sha1, "2b8b815229aa8a61e483fb4ba0588b8b6c491890")

    @testmode.integration
    def test_bundle_get_presigned(self):
        self._test_bundle_get_presigned(Replica.aws, True)
        self._test_bundle_get_presigned(Replica.aws, False)
        self._test_bundle_get_presigned(Replica.gcp, True)
        self._test_bundle_get_presigned(Replica.gcp, False)

    def _test_bundle_get_presigned(self, replica: Replica, explicit_version: bool):
        with self.subTest(f"{replica} {explicit_version}"):
            bundle_uuid = "011c7340-9b3c-4d62-bf49-090d79daf198"
            version = "2017-06-20T214506.766634Z"
            url = UrlBuilder().set(path="/v1/bundles/" + bundle_uuid)
            url.add_query("replica", replica.name)
            url.add_query("presignedurls", "true")
            if explicit_version:
                url.add_query("version", version)

            with override_bucket_config(BucketConfig.TEST_FIXTURE):
                resp_obj = self.assertGetResponse(
                    str(url),
                    requests.codes.ok,
                    redirect_follow_retries=BUNDLE_GET_RETRY_COUNT,
                    min_retry_interval_header=RETRY_AFTER_INTERVAL,
                    override_retry_interval=1,
                )

            presigned_url = resp_obj.json['bundle']['files'][0]['url']
            resp = requests.get(presigned_url)
            contents = resp.content

            hasher = hashlib.sha1()
            hasher.update(contents)
            sha1 = hasher.hexdigest()
            self.assertEqual(sha1, "2b8b815229aa8a61e483fb4ba0588b8b6c491890")

    @testmode.standalone
    def test_bundle_get_directurl_and_presigned(self):
        self._test_bundle_get_directurl_and_presigned(Replica.aws)
        self._test_bundle_get_directurl_and_presigned(Replica.gcp)

    def _test_bundle_get_directurl_and_presigned(self, replica: Replica):
        with self.subTest(replica):
            bundle_uuid = "011c7340-9b3c-4d62-bf49-090d79daf198"
            version = "2017-06-20T214506.766634Z"

            url = str(UrlBuilder()
                      .set(path="/v1/bundles/" + bundle_uuid)
                      .add_query("replica", replica.name)
                      .add_query("version", version)
                      .add_query("directurls", "true")
                      .add_query("presignedurls", "true"))

            with override_bucket_config(BucketConfig.TEST_FIXTURE):
                resp_obj = self.assertGetResponse(
                    url,
                    requests.codes.bad_request)
                self.assertEqual(resp_obj.json['code'], "only_one_urltype")

    @testmode.standalone
    def test_bundle_get_deleted(self):
        uuid = "deadbeef-0000-4a6b-8f0d-a7d2105c23be"
        version = "2017-12-05T235850.950361Z"
        # whole bundle delete
        self._test_bundle_get_deleted(Replica.aws, uuid, version, None)
        self._test_bundle_get_deleted(Replica.gcp, uuid, version, None)
        # get latest undeleted version
        uuid = "deadbeef-0001-4a6b-8f0d-a7d2105c23be"
        expected_version = "2017-12-05T235728.441373Z"
        self._test_bundle_get_deleted(Replica.aws, uuid, None, expected_version)
        self._test_bundle_get_deleted(Replica.gcp, uuid, None, expected_version)
        # specific version delete
        version = "2017-12-05T235850.950361Z"
        self._test_bundle_get_deleted(Replica.aws, uuid, version, None)
        self._test_bundle_get_deleted(Replica.gcp, uuid, version, None)

    def _test_bundle_get_deleted(self,
                                 replica: Replica,
                                 bundle_uuid: str,
                                 version: typing.Optional[str],
                                 expected_version: typing.Optional[str]):
        with self.subTest(f"{replica} {bundle_uuid} {version} {expected_version}"):
            with override_bucket_config(BucketConfig.TEST_FIXTURE):
                bundle_metadata = get_bundle_manifest(
                    uuid=bundle_uuid,
                    replica=replica,
                    version=version,
                    bucket=None,
                )
                bundle_version = None if bundle_metadata is None else bundle_metadata['version']
            self.assertEquals(
                bundle_version,
                expected_version
            )

    @testmode.standalone
    def test_bundle_get_checkout(self):
        self._test_bundle_get_checkout(Replica.aws, self.s3_test_fixtures_bucket, self.s3_test_bucket)
        self._test_bundle_get_checkout(Replica.gcp, self.gs_test_fixtures_bucket, self.gs_test_bucket)

    def _test_bundle_get_checkout(self, replica: Replica, test_fixtures_bucket: str, test_bucket: str):
        schema = replica.storage_schema
        handle = Config.get_blobstore_handle(replica)

        # upload test bundle from test fixtures bucket
        bundle_uuid = str(uuid.uuid4())
        file_uuid_1 = str(uuid.uuid4())
        file_uuid_2 = str(uuid.uuid4())
        filenames = ["file_1", "file_2"]
        resp_obj_1 = self.upload_file_wait(
            f"{schema}://{test_fixtures_bucket}/test_good_source_data/0",
            replica,
            file_uuid_1,
            bundle_uuid=bundle_uuid,
        )
        resp_obj_2 = self.upload_file_wait(
            f"{schema}://{test_fixtures_bucket}/test_good_source_data/1",
            replica,
            file_uuid_2,
            bundle_uuid=bundle_uuid,
        )
        file_version_1 = resp_obj_1.json['version']
        file_version_2 = resp_obj_2.json['version']

        # generate blob keys
        file_metadata = json.loads(
            handle.get(
                test_bucket,
                f"files/{file_uuid_1}.{file_version_1}"
            ).decode("utf-8"))
        file_key_1 = compose_blob_key(file_metadata)
        file_metadata = json.loads(
            handle.get(
                test_bucket,
                f"files/{file_uuid_2}.{file_version_2}"
            ).decode("utf-8"))
        file_key_2 = compose_blob_key(file_metadata)

        bundle_version = datetime_to_version_format(datetime.datetime.utcnow())
        self.put_bundle(
            replica,
            bundle_uuid,
            [(file_uuid_1, file_version_1, filenames[0]), (file_uuid_2, file_version_2, filenames[1])],
            bundle_version,
        )

        url = str(UrlBuilder()
                  .set(path="/v1/bundles/" + bundle_uuid)
                  .add_query("replica", replica.name)
                  .add_query("version", bundle_version)
                  .add_query("presignedurls", "true"))

        @eventually(10, 2)
        def assert_creation_dates_updated(prev_creation_dates):
            creation_dates = list(blob[1] for blob in handle.list(replica.checkout_bucket, bundle_uuid))
            self.assertTrue(creation_dates[i] > prev_creation_dates[i] for i in range(len(creation_dates)))

        def force_checkout():
            handle.copy(test_bucket, file_key_1,
                        replica.checkout_bucket, f"bundles/{bundle_uuid}.{bundle_version}/file_1")
            handle.copy(test_bucket, file_key_2,
                        replica.checkout_bucket, f"bundles/{bundle_uuid}.{bundle_version}/file_2")

        with override_bucket_config(BucketConfig.TEST), \
                mock.patch("dss.storage.checkout.bundle.start_bundle_checkout") as mock_start_bundle_checkout, \
                mock.patch("dss.storage.checkout.bundle.get_bundle_checkout_status") as mock_get_bundle_checkout_status:
            mock_start_bundle_checkout.return_value = 1
            mock_get_bundle_checkout_status.return_value = {'status': "RUNNING"}
            with self.subTest(f"{replica}: Initiate checkout and return 301 if bundle has not been checked out"):
                # assert 301 redirect on first GET
                self.assertGetResponse(url, requests.codes.moved, redirect_follow_retries=0)
                mock_start_bundle_checkout.assert_called_once_with(replica,
                                                                   bundle_uuid,
                                                                   bundle_version,
                                                                   dst_bucket=replica.checkout_bucket)
                force_checkout()
                # assert 200 on subsequent GET
                self.assertGetResponse(url, requests.codes.ok, redirect_follow_retries=5, override_retry_interval=0.5)
                mock_start_bundle_checkout.reset_mock()

            with self.subTest(f"{replica}: Initiate checkout and return 301 if file is missing from checkout bundle"):
                handle.delete(replica.checkout_bucket, f"bundles/{bundle_uuid}.{bundle_version}/file_1")
                # assert 301 redirect on first GET
                self.assertGetResponse(url, requests.codes.moved, redirect_follow_retries=0)
                mock_start_bundle_checkout.assert_called_once_with(replica,
                                                                   bundle_uuid,
                                                                   bundle_version,
                                                                   dst_bucket=replica.checkout_bucket)
                force_checkout()
                # assert 200 on subsequent GET
                self.assertGetResponse(url, requests.codes.ok, redirect_follow_retries=5, override_retry_interval=0.5)
                mock_start_bundle_checkout.reset_mock()

            with self.subTest(f"{replica}: Initiate checkout and return 200 if a file in checkout bundle is stale"):
                now = datetime.datetime.now(datetime.timezone.utc)
                previous_creation_dates = list(blob[1] for blob in handle.list(replica.checkout_bucket, bundle_uuid))
                stale_creation_date = now - datetime.timedelta(days=int(os.environ['DSS_BLOB_PUBLIC_TTL_DAYS']),
                                                               hours=1,
                                                               minutes=5)
                with mock.patch("dss.storage.checkout.bundle._list_checkout_bundle") as mock_list_checkout_bundle:
                    mock_list_checkout_bundle.return_value = list(
                        ((f"bundles/{bundle_uuid}.{bundle_version}/{filename}",
                         {BlobMetadataField.CREATED: stale_creation_date if i == 1 else now})
                         for i, filename in enumerate(filenames))
                    )
                    self.assertGetResponse(url, requests.codes.ok, redirect_follow_retries=0)
                mock_start_bundle_checkout.assert_called_once_with(replica,
                                                                   bundle_uuid,
                                                                   bundle_version,
                                                                   dst_bucket=replica.checkout_bucket)
                force_checkout()
                assert_creation_dates_updated(previous_creation_dates)
                mock_start_bundle_checkout.reset_mock()

            with self.subTest(
                    f"{replica}: Initiate checkout and return 301 if a file in checkout bundle is nearly expired"):
                now = datetime.datetime.now(datetime.timezone.utc)
                near_expired_creation_date = now - datetime.timedelta(days=int(os.environ['DSS_BLOB_TTL_DAYS']),
                                                                      minutes=-10)

                get_listing_fn = ("cloud_blobstore.s3.S3PagedIter.get_listing_from_response"
                                  if replica.name == "aws"
                                  else "cloud_blobstore.gs.GSPagedIter.get_listing_from_response")
                with mock.patch(get_listing_fn) as mock_get_listing:
                    mock_get_listing.return_value = (
                        (f"bundles/{bundle_uuid}.{bundle_version}/{filename}",
                         {BlobMetadataField.CREATED: near_expired_creation_date if i == 0 else now})
                        for i, filename in enumerate(filenames)
                    )
                    self.assertGetResponse(url, requests.codes.moved, redirect_follow_retries=0)
                mock_start_bundle_checkout.assert_called_once_with(replica,
                                                                   bundle_uuid,
                                                                   bundle_version,
                                                                   dst_bucket=replica.checkout_bucket)
                force_checkout()
                self.assertGetResponse(url, requests.codes.ok, redirect_follow_retries=5, override_retry_interval=0.5)
                mock_start_bundle_checkout.reset_mock()

            handle.delete(test_bucket, f"bundles/{bundle_uuid}.{bundle_version}")
            handle.delete(replica.checkout_bucket, f"bundles/{bundle_uuid}.{bundle_version}")

    @testmode.standalone
    def test_bundle_put(self):
        self._test_bundle_put(Replica.aws, self.s3_test_fixtures_bucket)
        self._test_bundle_put(Replica.gcp, self.gs_test_fixtures_bucket)

    def _test_bundle_put(self, replica: Replica, fixtures_bucket: str):
        schema = replica.storage_schema
        bundle_uuid = str(uuid.uuid4())
        file_uuid = str(uuid.uuid4())
        missing_file_uuid = str(uuid.uuid4())
        resp_obj = self.upload_file_wait(
            f"{schema}://{fixtures_bucket}/test_good_source_data/0",
            replica,
            file_uuid,
            bundle_uuid=bundle_uuid,
        )
        file_version = resp_obj.json['version']

        with self.subTest(f'{replica}: first bundle.'):
            bundle_version = datetime_to_version_format(datetime.datetime.utcnow())
            self.put_bundle(
                replica,
                bundle_uuid,
                [(file_uuid, file_version, "LICENSE")],
                bundle_version,
            )

        with self.subTest(f'{replica}: should be able to do this twice (i.e. same payload, same UUIDs)'):
            self.put_bundle(
                replica,
                bundle_uuid,
                [(file_uuid, file_version, "LICENSE")],
                bundle_version,
                requests.codes.ok,
            )

        with self.subTest(f'{replica}: should *NOT* be able to do this twice with different payload.'):
            self.put_bundle(
                replica,
                bundle_uuid,
                [(file_uuid, file_version, "LICENSE1")],
                bundle_version,
                requests.codes.conflict,
            )

        with self.subTest(f'{replica}: should *NOT* be able to do this without bundle version.'):
            self.put_bundle(
                replica,
                bundle_uuid,
                [(file_uuid, file_version, "LICENSE")],
                expected_code=requests.codes.bad_request
            )

        with self.subTest(f'{replica}: put fails when the bundle contains a duplicated file name.'):
            with nestedcontext.bind(time_left=lambda: 0):
                bundle_version = datetime_to_version_format(datetime.datetime.utcnow())
                bundle_uuid2 = str(uuid.uuid4())
                file_uuid2 = str(uuid.uuid4())
                resp_obj2 = self.upload_file_wait(
                    f"{schema}://{fixtures_bucket}/test_good_source_data/0",
                    replica,
                    file_uuid2,
                    bundle_uuid=bundle_uuid2,
                )
                file_version2 = resp_obj2.json['version']
                resp = self.put_bundle(
                    replica,
                    bundle_uuid2,
                    [(file_uuid, file_version, "LICENSE"), (file_uuid2, file_version2, "LICENSE")],
                    bundle_version,
                    expected_code=requests.codes.bad_request
                )
                self.assertEqual(json.loads(resp.body)['code'], 'duplicate_filename')

        with self.subTest(f'{replica}: put fails when an invalid bundle_uuid is supplied.'):
            bundle_version = datetime_to_version_format(datetime.datetime.utcnow())
            self.put_bundle(
                replica,
                "12345",
                [(file_uuid, file_version, "LICENSE")],
                bundle_version,
                expected_code=requests.codes.bad_request
            )

        with self.subTest(f'{replica}: put bundle fails when an invalid version is supplied'):
            self.put_bundle(
                replica,
                bundle_uuid,
                [(file_uuid, file_version, "LICENSE")],
                "ABCD",
                expected_code=requests.codes.bad_request
            )

        with self.subTest(f'{replica}: should *NOT* be able to upload a bundle with a missing file, but we should get '
                          'requests.codes.bad.'):
            with nestedcontext.bind(time_left=lambda: 0):
                bundle_version = datetime_to_version_format(datetime.datetime.utcnow())
                resp_obj = self.put_bundle(
                    replica,
                    bundle_uuid,
                    [
                        (file_uuid, file_version, "LICENSE0"),
                        (missing_file_uuid, file_version, "LICENSE1"),
                    ],
                    bundle_version,
                    expected_code=requests.codes.bad
                )
                self.assertEqual(resp_obj.json['code'], "file_missing")

        with self.subTest(f'{replica}: uploads a file, but delete the file metadata. put it back after a delay.'):
            self.upload_file_wait(
                f"{schema}://{fixtures_bucket}/test_good_source_data/0",
                replica,
                missing_file_uuid,
                file_version,
                bundle_uuid=bundle_uuid
            )
            handle = Config.get_blobstore_handle(replica)
            bucket = replica.bucket
            file_metadata = handle.get(bucket, f"files/{missing_file_uuid}.{file_version}")
            handle.delete(bucket, f"files/{missing_file_uuid}.{file_version}")

            class UploadThread(threading.Thread):
                def run(innerself):
                    time.sleep(5)
                    data_fh = io.BytesIO(file_metadata)
                    handle.upload_file_handle(bucket, f"files/{missing_file_uuid}.{file_version}", data_fh)

            # start the upload (on a delay...)
            upload_thread = UploadThread()
            upload_thread.start()

            # this should at first fail to find one of the files, but the UploadThread will eventually upload the file
            # metadata.  since we give the upload bundle process ample time to spin, it should eventually find the file
            # metadata and succeed.
            with nestedcontext.bind(time_left=lambda: sys.maxsize):
                bundle_version = datetime_to_version_format(datetime.datetime.utcnow())
                self.put_bundle(
                    replica,
                    bundle_uuid,
                    [
                        (file_uuid, file_version, "LICENSE0"),
                        (missing_file_uuid, file_version, "LICENSE1"),
                    ],
                    bundle_version,
                    expected_code=requests.codes.created,
                )

    @testmode.standalone
    def test_bundle_delete(self):
        tests = [
            (Replica.aws, self.s3_test_fixtures_bucket, True),
            (Replica.gcp, self.gs_test_fixtures_bucket, True),
            (Replica.aws, self.s3_test_fixtures_bucket, False),
            (Replica.gcp, self.gs_test_fixtures_bucket, False)
        ]
        for test in tests:
            with self.subTest(f"{test[0].name}, {test[2]}"):
                self._test_bundle_delete(*test)

    def _test_bundle_delete(self, replica: Replica, fixtures_bucket: str, authorized: bool):
        schema = replica.storage_schema

        # prep existing bundle
        bundle_uuid = str(uuid.uuid4())
        file_uuid = str(uuid.uuid4())
        resp_obj = self.upload_file_wait(
            f"{schema}://{fixtures_bucket}/test_good_source_data/0",
            replica,
            file_uuid,
            bundle_uuid=bundle_uuid,
        )
        file_version = resp_obj.json['version']

        bundle_version = datetime_to_version_format(datetime.datetime.utcnow())
        self.put_bundle(
            replica,
            bundle_uuid,
            [(file_uuid, file_version, "LICENSE")],
            bundle_version,
        )

        handle = Config.get_blobstore_handle(replica)
        bucket = replica.bucket

        self.delete_bundle(replica, bundle_uuid, authorized=authorized)
        tombstone_exists = test_object_exists(handle, bucket, f"bundles/{bundle_uuid}.dead")
        self.assertEquals(tombstone_exists, authorized)

        self.delete_bundle(replica, bundle_uuid, bundle_version, authorized=authorized)
        tombstone_exists = test_object_exists(handle, bucket, f"bundles/{bundle_uuid}.{bundle_version}.dead")
        self.assertEquals(tombstone_exists, authorized)

    @testmode.standalone
    def test_no_replica(self):
        """
        Verify we raise the correct error code when we provide no replica.
        """
        bundle_uuid = "ce55fd51-7833-469b-be0b-5da88ec0ffee"

        url = str(UrlBuilder().set(path="/v1/bundles/" + bundle_uuid))

        with override_bucket_config(BucketConfig.TEST_FIXTURE):
            self.assertPutResponse(
                url,
                requests.codes.bad_request,
                json_request_body=dict(
                    files=[],
                    creator_uid=12345,
                ),
                expected_error=ExpectedErrorFields(
                    code="illegal_arguments",
                    status=requests.codes.bad_request,
                    expect_stacktrace=True)
            )

    @testmode.standalone
    def test_no_files(self):
        """
        Verify we raise the correct error code when we do not provide the list of files.
        """
        bundle_uuid = "ce55fd51-7833-469b-be0b-5da88ec0ffee"

        url = str(UrlBuilder()
                  .set(path="/v1/bundles/" + bundle_uuid)
                  .add_query("replica", "aws"))

        with override_bucket_config(BucketConfig.TEST_FIXTURE):
            self.assertPutResponse(
                url,
                requests.codes.bad_request,
                json_request_body=dict(
                    creator_uid=12345,
                ),
                expected_error=ExpectedErrorFields(
                    code="illegal_arguments",
                    status=requests.codes.bad_request,
                    expect_stacktrace=True)
            )

    @testmode.standalone
    def test_bundle_get_not_found(self):
        """
        Verify that we return the correct error message when the bundle cannot be found.
        """
        self._test_bundle_get_not_found(Replica.aws)
        self._test_bundle_get_not_found(Replica.gcp)

    def _test_bundle_get_not_found(self, replica: Replica):
        with self.subTest(replica):
            bundle_uuid = str(uuid.uuid4())

            url = str(UrlBuilder()
                      .set(path="/v1/bundles/" + bundle_uuid)
                      .add_query("replica", replica.name))

            with override_bucket_config(BucketConfig.TEST_FIXTURE):
                self.assertGetResponse(
                    url,
                    requests.codes.not_found,
                    expected_error=ExpectedErrorFields(
                        code="not_found",
                        status=requests.codes.not_found)
                )

            version = "2017-06-16T193604.240704Z"
            url = str(UrlBuilder()
                      .set(path="/v1/bundles/" + bundle_uuid)
                      .add_query("replica", replica.name)
                      .add_query("version", version))

            with override_bucket_config(BucketConfig.TEST_FIXTURE):
                self.assertGetResponse(
                    url,
                    requests.codes.not_found,
                    expected_error=ExpectedErrorFields(
                        code="not_found",
                        status=requests.codes.not_found)
                )

    def put_bundle(
            self,
            replica: Replica,
            bundle_uuid: str,
            files: typing.Iterable[typing.Tuple[str, str, str]],
            bundle_version: typing.Optional[str] = None,
            expected_code: int = requests.codes.created):
        builder = UrlBuilder().set(path="/v1/bundles/" + bundle_uuid).add_query("replica", replica.name)
        if bundle_version:
            builder.add_query("version", bundle_version)
        url = str(builder)

        resp_obj = self.assertPutResponse(
            url,
            expected_code,
            json_request_body=dict(
                files=[
                    dict(
                        uuid=file_uuid,
                        version=file_version,
                        name=file_name,
                        indexed=False,
                    )
                    for file_uuid, file_version, file_name in files
                ],
                creator_uid=12345,
            ),
        )

        if 200 <= resp_obj.response.status_code < 300:
            self.assertHeaders(
                resp_obj.response,
                {
                    'content-type': "application/json",
                }
            )
            self.assertIn('version', resp_obj.json)
        return resp_obj

    def delete_bundle(
            self,
            replica: Replica,
            bundle_uuid: str,
            bundle_version: typing.Optional[str]=None,
            authorized: bool=True):
        # make delete request
        url_builder = UrlBuilder().set(path="/v1/bundles/" + bundle_uuid).add_query('replica', replica.name)
        if bundle_version:
            url_builder = url_builder.add_query('version', bundle_version)
        url = str(url_builder)

        json_request_body = dict(reason="reason")
        if bundle_version:
            json_request_body['version'] = bundle_version

        expected_code = requests.codes.ok if authorized else requests.codes.forbidden

        # delete and check results
        return self.assertDeleteResponse(
            url,
            expected_code,
            json_request_body=json_request_body,
            headers=get_auth_header(authorized=authorized),
        )


if __name__ == '__main__':
    unittest.main()
