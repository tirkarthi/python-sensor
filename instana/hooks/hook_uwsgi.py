"""
The uwsgi and uwsgidecorators packages are added automatically to the Python environment
when running under uWSGI.  Here we attempt to detect the presence of these packages and
then use the appropriate hooks.
"""
from __future__ import absolute_import

from ..log import logger
from ..singletons import agent

try:
    import uwsgi
    logger.debug("uWSGI options: %s", uwsgi.opt)

    opt_master = uwsgi.opt.get('master', False)
    opt_lazy_apps = uwsgi.opt.get('lazy-apps', False)

    if uwsgi.opt.get('enable-threads', False) is False:
        logger.warn("Required: uWSGI threads are not enabled.  " +
                    "Please enable by using the uWSGI --enable-threads option.")

    if opt_master and opt_lazy_apps is False:
        # --master is supplied in uWSGI options (otherwise uwsgidecorators package won't be available)
        # When --lazy-apps is True, this postfork hook isn't needed
        import uwsgidecorators

        @uwsgidecorators.postfork
        def uwsgi_handle_fork():
            """ This is our uWSGI hook to detect and act when worker processes are forked off. """
            logger.debug("Handling uWSGI fork...")
            agent.handle_fork()

        logger.debug("Applied uWSGI hooks")
    else:
        logger.debug("uWSGI --master=%s --lazy-apps=%s: postfork hooks not applied", opt_master, opt_lazy_apps)
except ImportError as e:
    logger.debug('uwsgi hooks: decorators not available: %s', e)
    pass
