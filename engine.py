"""
Copyright (c) 2012 Shotgun Software, Inc
----------------------------------------------------

A Maya engine for Tank.

"""

import tank
import platform
import sys
import traceback
import textwrap
import os
import maya.OpenMaya as OpenMaya
import pymel.core as pm
import maya.cmds as cmds
import maya
from pymel.core import Callback

CONSOLE_OUTPUT_WIDTH = 200

###############################################################################################
# methods to support the state when the engine cannot start up
# for example if a non-tank file is loaded in maya  

class SceneEventWatcher(object):
    """
    Encapsulates event handling for multiple scene events and routes them
    into a single callback.
    
    This uses OpenMaya.MSceneMessage rather than scriptJobs as the former 
    can safely be removed from inside of the callback itself
    
    Specifying run_once=True in the constructor causes all events to be
    cleaned up after the first one has triggered
    """
    def __init__(self, cb_fn,  
                 scene_events = [OpenMaya.MSceneMessage.kAfterOpen, 
                                 OpenMaya.MSceneMessage.kAfterSave,
                                 OpenMaya.MSceneMessage.kAfterNew],
                 run_once=False):
        self.__message_ids = []
        self.__cb_fn = cb_fn
        self.__scene_events = scene_events
        self.__run_once=run_once

        # register scene event callbacks:                                
        self.start_watching()

    def start_watching(self):
        # if currently watching then stop:
        self.stop_watching()
        
        # now add callbacks to watch for some scene events:
        for ev in self.__scene_events:
            try:
                msg_id = OpenMaya.MSceneMessage.addCallback(ev, SceneEventWatcher.__scene_event_callback, self);
            except Exception, e:
                # report warning...
                continue    
            self.__message_ids.append(msg_id);

        # create a callback that will be run when Maya 
        # exits so we can do some clean-up:
        msg_id = OpenMaya.MSceneMessage.addCallback(OpenMaya.MSceneMessage.kMayaExiting, SceneEventWatcher.__maya_exiting_callback, self)
        self.__message_ids.append(msg_id);

    def stop_watching(self):
        for msg_id in self.__message_ids:
            OpenMaya.MMessage.removeCallback(msg_id)
        self.__message_ids = []

    @staticmethod
    def __scene_event_callback(watcher):
        """
        Called on a scene event:
        """
        if watcher.__run_once:
            watcher.stop_watching()
        watcher.__cb_fn()

    @staticmethod
    def __maya_exiting_callback(watcher):
        """
        Called on Maya exit - should clean up any existing calbacks
        """
        watcher.stop_watching()

def refresh_engine(engine_name, prev_context):
    """
    refresh the current engine
    """    
    current_engine = tank.platform.current_engine()
    
    # first make sure that the disabled menu is reset, if it exists...
    if pm.menu("TankMenuDisabled", exists=True):
        pm.deleteUI("TankMenuDisabled")
    
    # if the scene opened is actually a file->new, then maintain the current
    # context/engine.
    if pm.sceneName() == "":
        return current_engine

    new_path = pm.sceneName().abspath()
    
    # this file could be in another project altogether, so create a new Tank
    # API instance.
    try:
        tk = tank.tank_from_path(new_path)
    except tank.TankError, e:
        OpenMaya.MGlobal.displayInfo("Tank Engine cannot be started: %s" % e)
        # render menu
        create_tank_disabled_menu()
        
        # (AD) - this leaves the engine running - is this correct?        
        return current_engine

    ctx = tk.context_from_path(new_path, prev_context)
    
    # if an engine is active right now and context is unchanged, no need to 
    # rebuild the same engine again!
    if current_engine is not None and ctx == prev_context:
        return current_engine
    
    if current_engine:
        current_engine.log_debug("Ready to switch to context because of scene event !")
        current_engine.log_debug("Prev context: %s" % prev_context)   
        current_engine.log_debug("New context: %s" % ctx)
        # tear down existing engine
        current_engine.destroy()
    
    # create new engine
    try:
        new_engine = tank.platform.start_engine(engine_name, tk, ctx)
    except tank.TankEngineInitError, e:
        OpenMaya.MGlobal.displayInfo("Tank Engine cannot be started: %s" % e)
        
        # render menu
        create_tank_disabled_menu()

        return None
    else:
        new_engine.log_debug("Launched new engine for context!")
        
    return new_engine
        
def on_scene_event_callback(engine_name, prev_context):
    """
    Callback that's run whenever a scene is saved or opened.
    """
    new_engine = None
    try:        
        new_engine = refresh_engine(engine_name, prev_context)
    except Exception, e:
        (exc_type, exc_value, exc_traceback) = sys.exc_info()
        message = ""
        message += "Message: There was a problem starting the Tank Engine.\n"
        message += "Please contact tanksupport@shotgunsoftware.com\n\n"
        message += "Exception: %s - %s\n" % (exc_type, exc_value)
        message += "Traceback (most recent call last):\n"
        message += "\n".join( traceback.format_tb(exc_traceback))
        OpenMaya.MGlobal.displayError(message) 
        new_engine = None
    
    if not new_engine:
        # don't have an engine but still want to watch for 
        # future scene events:
        cb_fn = lambda en=engine_name, pc=prev_context:on_scene_event_callback(en, pc)
        SceneEventWatcher(cb_fn, run_once=True)

def tank_disabled_message():
    """
    Explain why tank is disabled.
    """
    msg = ("Tank is disabled because it cannot recongnize the currently opened file. "
           "Try opening another file or restarting Maya.")
    
    cmds.confirmDialog( title="Tank is disabled", 
                message=msg, 
                button=["Ok"], 
                defaultButton="Ok", 
                cancelButton="Ok", 
                dismissString="Ok" )
        
    
def create_tank_disabled_menu():
    """
    Render a special "tank is disabled menu"
    """
    if pm.menu("TankMenu", exists=True):
        pm.deleteUI("TankMenu")

    sg_menu = pm.menu("TankMenuDisabled", label="Tank", parent=pm.melGlobals["gMainWindow"])
    pm.menuItem(label="Tank is disabled.", parent=sg_menu, 
                command=lambda arg: tank_disabled_message())


###############################################################################################
# The Tank Maya engine

class MayaEngine(tank.platform.Engine):
    
    ##########################################################################################
    # init and destroy
            
    def init_engine(self):
        self.log_debug("%s: Initializing..." % self)
        
        # keep handles to all qt dialogs to help GC
        self.__created_qt_dialogs = []
                
        # check that we are running an ok version of maya
        current_os = cmds.about(operatingSystem=True)
        if current_os not in ["mac", "win64", "linux64"]:
            raise tank.TankError("The current platform is not supported! Supported platforms "
                                 "are Mac, Linux 64 and Windows 64.")
        
        current_maya_version = cmds.about(version=True)
        if current_maya_version.startswith("2012") or current_maya_version.startswith("2013"):
            self.log_debug("Running Maya version %s" % current_maya_version)
        else:
            raise tank.TankError("Your version of Maya is not supported. Currently, Tank only "
                                 "supports 2012 and 2013.") 
                
        if self.context.project is None:
            # must have at least a project in the context to even start!
            raise tank.TankError("The Tank engine needs at least a project in the context "
                                 "in order to start! Your context: %s" % self.context)

        # our job queue
        self._queue = []
                  
        # Set the Maya project based on config
        self._set_project()
       
        # add qt paths and dlls
        self._init_pyside()
                  
        # need to watch some scene events in case the engine needs rebuilding:
        cb_fn = lambda en=self.instance_name, pc=self.context:on_scene_event_callback(en, pc)
        self.__watcher = SceneEventWatcher(cb_fn)
        self.log_debug("Registered open and save callbacks.")
                                
    def post_app_init(self):
        """
        Called when all apps have initialized
        """    
        # detect if in batch mode
        if self.has_ui:
            self._menu_handle = pm.menu("TankMenu", label="Tank", parent=pm.melGlobals["gMainWindow"])
            # create our menu handler
            tk_maya = self.import_module("tk_maya")
            self._menu_generator = tk_maya.MenuGenerator(self, self._menu_handle)
            # hook things up so that the menu is created every time it is clicked
            self._menu_handle.postMenuCommand(self._menu_generator.create_menu)
    
    def destroy_engine(self):
        self.log_debug("%s: Destroying..." % self)
        
        # stop watching scene events:
        self.__watcher.stop_watching()
        
        # clean up UI:
        if pm.menu(self._menu_handle, exists=True):
            pm.deleteUI(self._menu_handle)
    
    def _init_pyside(self):
        """
        Handles the pyside init
        """
        
        # first see if pyside is already present - in that case skip!
        try:
            from PySide import QtGui
        except:
            # fine, we don't expect pyside to be present just yet
            self.log_debug("PySide not detected - Tank will add it to the setup now...")
        else:
            # looks like pyside is already working! No need to do anything
            self.log_debug("PySide detected - Tank will use the existing version.")
            return
        
        
        if sys.platform == "darwin":
            pyside_path = os.path.join(self.disk_location, "resources","pyside112_py26_qt471_mac", "python")
            sys.path.append(pyside_path)
        
        elif sys.platform == "win32":
            pyside_path = os.path.join(self.disk_location, "resources","pyside111_py26_qt471_win64", "python")
            sys.path.append(pyside_path)
            dll_path = os.path.join(self.disk_location, "resources","pyside111_py26_qt471_win64", "lib")
            path = os.environ.get("PATH", "")
            path += ";%s" % dll_path
            os.environ["PATH"] = path
            
        elif sys.platform == "linux2":        
            pyside_path = os.path.join(self.disk_location, "resources","pyside112_py26_qt471_linux", "python")
            sys.path.append(pyside_path)
        
        else:
            self.log_error("Unknown platform - cannot initialize PySide!")
        
        # now try to import it
        try:
            from PySide import QtGui
        except Exception, e:
            self.log_error("PySide could not be imported! Tank Apps using pyside will not "
                           "operate correctly! Error reported: %s" % e)
     
    def show_dialog(self, title, bundle, widget_class, *args, **kwargs):
        """
        Shows a non-modal dialog window in a way suitable for this engine. 
        The engine will attempt to parent the dialog nicely to the host application.
        
        :param title: The title of the window
        :param bundle: The app, engine or framework object that is associated with this window
        :param widget_class: The class of the UI to be constructed. This must derive from QWidget.
        
        Additional parameters specified will be passed through to the widget_class constructor.
        
        :returns: the created widget_class instance
        """

        from maya import cmds as mc
        import maya.OpenMayaUI as omui
        from PySide import QtCore, QtGui
        import shiboken
        from tank.platform.qt import tankqdialog
                
        # 1. use maya to create the window - this ensures parenting works as expected:
        # (window options match the default options for a standard QDialog)
        maya_win = mc.window(title=("Tank: %s" % title), minimizeButton=False)
        
        # 2. find the QWidget for this window:
        qt_win_ptr = omui.MQtUtil.findWindow(maya_win)
        qt_win = shiboken.wrapInstance(long(qt_win_ptr), QtGui.QWidget)
        
        # 3. construct the widget object 
        obj = widget_class(*args, **kwargs)

        # 4. create the interior form directly parented to the maya window:
        main_form = tankqdialog.TankMainForm(title, bundle, obj, qt_win)

        # 5. fix up the layout so it does something sensible
        layout = QtGui.QVBoxLayout()
        layout.addWidget(main_form)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        qt_win.setLayout(layout)
        
        # 6. make sure the dialog window is deleted if the internal widget is closed:
        def on_delete_widget(exit_code):
            # - sometimes python scares me!
            if cmds.window(maya_win, query=True, exists=True):
                cmds.deleteUI(maya_win)
        main_form.widget_closed.connect(on_delete_widget)
        
        # 7. and show the window:
        mc.showWindow(maya_win)
        
        # finally, return the instantiated widget
        return obj

    
    def show_modal(self, title, bundle, widget_class, *args, **kwargs):
        """
        Shows a modal dialog window in a way suitable for this engine. The engine will attempt to
        integrate it as seamlessly as possible into the host application. This call is blocking 
        until the user closes the dialog.
        
        :param title: The title of the window
        :param bundle: The app, engine or framework object that is associated with this window
        :param widget_class: The class of the UI to be constructed. This must derive from QWidget.
        
        Additional parameters specified will be passed through to the widget_class constructor.

        :returns: (a standard QT dialog status return code, the created widget_class instance)
        """
        from tank.platform.qt import tankqdialog 
        import maya.OpenMayaUI as OpenMayaUI
        from PySide import QtCore, QtGui
        import shiboken
        
        # first construct the widget object 
        obj = widget_class(*args, **kwargs)
        
        # now create a dialog to put it inside
        ptr = OpenMayaUI.MQtUtil.mainWindow()
        parent = shiboken.wrapInstance(long(ptr), QtGui.QMainWindow)
        self.log_debug("Parenting dialog to main window %08x %s" % (ptr, parent))
        dialog = tankqdialog.TankQDialog(title, bundle, obj, parent)
        
        # keep a reference to all created dialogs to make GC happy
        self.__created_qt_dialogs.append(dialog)
        
        # finally launch it, modal state        
        status = dialog.exec_()
        
        # lastly, return the instantiated class
        return (status, obj)

        
        
    @property
    def has_ui(self):
        """
        Detect and return if maya is running in batch mode
        """
        if cmds.about(batch=True):
            # batch mode or prompt mode
            return False
        else:
            return True        
    
    ##########################################################################################
    # logging
    
    def log_debug(self, msg):
        if self.get_setting("debug_logging", False):
            msg = "%s DEBUG: %s" % (self, msg)
            for l in textwrap.wrap(msg, CONSOLE_OUTPUT_WIDTH):
                OpenMaya.MGlobal.displayInfo(l)
    
    def log_info(self, msg):
        msg = "Tank: %s" % msg
        for l in textwrap.wrap(msg, CONSOLE_OUTPUT_WIDTH):
            OpenMaya.MGlobal.displayInfo(l)
        
    def log_warning(self, msg):
        msg = "Tank: %s" % msg
        for l in textwrap.wrap(msg, CONSOLE_OUTPUT_WIDTH):
            OpenMaya.MGlobal.displayWarning(l)
    
    def log_error(self, msg):
        msg = "Tank: %s" % msg
        OpenMaya.MGlobal.displayError(msg)
    
    ##########################################################################################
    # scene and project management            
        
    def _set_project(self):
        """
        Set the maya project
        """
        setting = self.get_setting("template_project")
        if setting is None:
            return

        tmpl = self.tank.templates.get(setting)
        fields = self.context.as_template_fields(tmpl)
        proj_path = tmpl.apply_fields(fields)
        self.log_info("Setting Maya project to '%s'" % proj_path)        
        pm.mel.setProject(proj_path)
    
    ##########################################################################################
    # queue

    def add_to_queue(self, name, method, args):
        """
        Maya implementation of the engine synchronous queue. Adds an item to the queue.
        """
        self.log_warning("The Engine Queue is now deprecated! Please contact support@shotgunsoftware.com")
        qi = {}
        qi["name"] = name
        qi["method"] = method
        qi["args"] = args
        self._queue.append(qi)
    
    def report_progress(self, percent):
        """
        Callback function part of the engine queue. This is being passed into the methods
        that are executing in the queue so that they can report progress back if they like
        """
        # convert to delta value before passing to maya
        delta = percent - self._current_progress
        pm.progressBar(self._maya_progress_bar, edit=True, step=delta)
        self._current_progress = percent
    
    def execute_queue(self):
        """
        Executes all items in the queue, one by one, in a controlled fashion
        """
        self.log_warning("The Engine Queue is now deprecated! Please contact support@shotgunsoftware.com")
        self._maya_progress_bar = maya.mel.eval('$tmp = $gMainProgressBar')
        
        # execute one after the other syncronously
        while len(self._queue) > 0:
            
            # take one item off
            current_queue_item = self._queue[0]
            self._queue = self._queue[1:]

            # set up the progress bar  
            pm.progressBar( self._maya_progress_bar,
                            edit=True,
                            beginProgress=True,
                            isInterruptable=False,
                            status=current_queue_item["name"] )
            self._current_progress = 0
            
            # process it
            try:
                kwargs = current_queue_item["args"]
                # force add a progress_callback arg - this is by convention
                kwargs["progress_callback"] = self.report_progress
                # execute
                current_queue_item["method"](**kwargs)
            except:
                # error and continue
                # todo: may want to abort here - or clear the queue? not sure.
                self.log_exception("Error while processing callback %s" % current_queue_item)
            finally:
                pm.progressBar(self._maya_progress_bar, edit=True, endProgress=True)
        
            

  
        
        
                
