import logging
import re, json, requests, urllib, urllib2, uuid

from django.core import serializers

import crypto
import settings
import kalite
from models import *

json_serializer = serializers.get_serializer("json")()


class SyncClient(object):
    """ This is for the distributed server, for establishing a client session with
    the central server.  Over that session, syncing can occur in multiple requests"""
     
    session = None
    counters_to_download = None
    counters_to_upload = None
    
    def __init__(self, host="%s://%s/"%(settings.SECURESYNC_PROTOCOL,settings.CENTRAL_SERVER_HOST), require_trusted=True):
        url = urllib2.urlparse.urlparse(host)
        self.url = "%s://%s" % (url.scheme, url.netloc)
        self.require_trusted = require_trusted
    
    def path_to_url(self, path):
        if path.startswith("/"):
            return self.url + path
        else:
            return self.url + "/securesync/api/" + path
    
    def post(self, path, payload={}, *args, **kwargs):
        if self.session and self.session.client_nonce:
            payload["client_nonce"] = self.session.client_nonce
        return requests.post(self.path_to_url(path), data=json.dumps(payload))

    def get(self, path, payload={}, *args, **kwargs):
        if self.session and self.session.client_nonce:
            payload["client_nonce"] = self.session.client_nonce
        # add a random parameter to ensure the request is not cached
        payload["_"] = uuid.uuid4().hex
        query = urllib.urlencode(payload)
        return requests.get(self.path_to_url(path) + "?" + query, *args, **kwargs)
        
    def test_connection(self):
        try:
            if self.get("test", timeout=5).content != "OK":
                return "bad_address"
            return "success"
        except requests.ConnectionError:
            return "connection_error"
        except Exception as e:
            return "error (%s)" % e
            
    def register(self):
        """Register this device with a zone."""
        
        # Get the required model data by registering (online and offline options available)
        try:
            if self.test_connection()=="success":
                certs = settings.INSTALL_CERTIFICATES if len(settings.INSTALL_CERTIFICATES)>0 else []
                models_json = self.register_online(certs=certs)
            elif getattr(settings, "INSTALL_CERTIFICATES", None):
                models_json = self.register_offline(settings.INSTALL_CERTIFICATES)
            else:
                return { "code": "offline_with_no_install_certificates" }
        except Exception as e:
            # Some of our exceptions are actually json blobs from the server.
            #   Try loading them to pass on that error info.
            try:
                return json.loads(e.message)
            except:
                return { "err", e.message }
        
        # If we got here, we've successfully registered, and 
        #   have the model data necessary for completing registration!
        for model in models:
            if not model.object.verify():
                logging.info("Failed to verify model!")
                import pdb; pdb.set_trace()
                
            # save the imported model, and mark the returned Device as trusted
            if isinstance(model.object, Device):
                model.object.save(is_trusted=True, imported=True)
            else:
                model.object.save(imported=True)
        
        # If that all completes successfully, then we've registered!  Woot!
        return {"code": "registered"}
#        return json.loads(r.content)    

    def register_offline(self, certs):
        """Register this device with a zone, using offline data"""

        raise NotImplementedError()
        
    def register_online(self, certs=[]):
        """Register this device with a zone, through the central server directly"""
        
        own_device = Device.get_own_device()

        if not certs or len(certs)==0:
            r = self.post("register", {
                "client_device": json_serializer.serialize([own_device], ensure_ascii=False),
                "install_certificate": cert
            })
        
        # Try certificates until we find one that worked!
        else:
            for cert in certs:
                import logging; logging.debug("\t%s",cert)
                r = self.post("register", {
                    "client_device": json_serializer.serialize([own_device], ensure_ascii=False),
                    "install_certificate": cert
                })
            
                if r.status_code == 200:
                    logging.info("Registered with install certificate %s" % cert)
                    break;

        # Failed to register with any certificate
        if r.status_code != 200:
            raise Exception(r.content)

        # When we register, we should receive the model information we require.
        return serializers.deserialize("json", r.content)
        
    
    def start_session(self):
        if self.session:
            self.close_session()
        self.session = SyncSession()
        self.session.client_nonce = uuid.uuid4().hex
        self.session.client_device = Device.get_own_device()
        r = self.post("session/create", {
            "client_nonce": self.session.client_nonce,
            "client_device": self.session.client_device.pk,
            "client_version": kalite.VERSION,
            "client_os": kalite.OS,
        })
        data = json.loads(r.content)
        if data.get("error", ""):
            raise Exception(data.get("error", ""))
        signature = data.get("signature", "")
        session = serializers.deserialize("json", data["session"]).next().object
        if not session.verify_server_signature(signature):
            raise Exception("Signature did not match.")
        if session.client_nonce != self.session.client_nonce:
            raise Exception("Client nonce did not match.")
        if session.client_device != self.session.client_device:
            raise Exception("Client device did not match.")
        if self.require_trusted and not session.server_device.get_metadata().is_trusted:
            raise Exception("The server is not trusted.")
        self.session.server_nonce = session.server_nonce
        self.session.server_device = session.server_device
        self.session.verified = True
        self.session.timestamp = session.timestamp
        self.session.save()

        r = self.post("session/create", {
            "client_nonce": self.session.client_nonce,
            "client_device": self.session.client_device.pk,
            "server_nonce": self.session.server_nonce,
            "server_device": self.session.server_device.pk,
            "signature": self.session.sign(),
        })
        
        if r.status_code == 200:
            return "success"
        else:
            return r
        
    def close_session(self):
        if not self.session:
            return
        self.post("session/destroy", {
            "client_nonce": self.session.client_nonce
        })
        self.session.delete()
        self.session = None
        return "success"

    def get_server_device_counters(self):
        r = self.get("device/counters")
        return json.loads(r.content or "{}").get("device_counters", {})
        
    def get_client_device_counters(self):
        return get_device_counters(self.session.client_device.get_zone())

    def sync_device_records(self):
        
        server_counters = self.get_server_device_counters()
        client_counters = self.get_client_device_counters()
        
        devices_to_download = []
        devices_to_upload = []
        
        self.counters_to_download = {}
        self.counters_to_upload = {}
        
        for device in client_counters:
            if device not in server_counters:
                devices_to_upload.append(device)
                self.counters_to_upload[device] = 0
            elif client_counters[device] > server_counters[device]:
                self.counters_to_upload[device] = server_counters[device]
        
        for device in server_counters:
            if device not in client_counters:
                devices_to_download.append(device)
                self.counters_to_download[device] = 0
            elif server_counters[device] > client_counters[device]:
                self.counters_to_download[device] = client_counters[device]
                
        response = json.loads(self.post("device/download", {"devices": devices_to_download}).content)
        download_results = save_serialized_models(response.get("devices", "[]"), increment_counters=False)
        
        self.session.models_downloaded += download_results["saved_model_count"]
        
        # TODO(jamalex): upload local devices as well? only needed once we have P2P syncing
        
    def sync_models(self):

        if self.counters_to_download is None or self.counters_to_upload is None:
            self.sync_device_records()

        response = json.loads(self.post("models/download", {"device_counters": self.counters_to_download}).content)
        download_results = save_serialized_models(response.get("models", "[]"))
        
        self.session.models_downloaded += download_results["saved_model_count"]
        
        response = self.post("models/upload", {"models": get_serialized_models(self.counters_to_upload)})
        upload_results = json.loads(response.content)
        
        self.session.models_uploaded += upload_results["saved_model_count"]
        
        self.counters_to_download = None
        self.counters_to_upload = None
        
        return {"download_results": download_results, "upload_results": upload_results}
